"""AWS IoT MQTT-over-WSS client.

Wraps paho-mqtt with asyncio bridging. Signs the connection URL with
SigV4 query-string presigning (arch.md §4b). One connection per device.

The signed URL has a 24h X-Amz-Expires baked in. AWS validates SigV4
only at handshake; once connected, the session persists past credential
expiry. We don't do scheduled reconnects — paho's auto-reconnect handles
disconnects, and we refresh creds + manually reconnect only on
persistent reconnect failure (caller's responsibility — the coordinator).
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .auth import CognitoAuth
from .protocol import wrap_envelope
from .sigv4 import presigned_ws_path

_LOGGER = logging.getLogger(__name__)

# Topic templates
_TOPIC_PBINPUT = "/device/{thing_name}/pbinput"
_TOPIC_PBOUTPUT = "/device/{thing_name}/pboutput"
_TOPIC_NOTIFY_APP = "/device/{thing_name}/notify-app"


class MqttClient:
    """Async-friendly wrapper around paho-mqtt for one Lymow device."""

    def __init__(
        self,
        thing_name: str,
        host: str,
        region: str,
        auth: CognitoAuth,
        on_pboutput: Callable[[bytes], None],
        on_notify_app: Callable[[dict], None],
        on_disconnect_async: Callable[[], Any] | None = None,
    ) -> None:
        """on_disconnect_async may be either a sync callable or an async coroutine
        function — both forms are supported via asyncio.ensure_future.
        """
        self._thing_name = thing_name
        self._host = host
        self._region = region
        self._auth = auth
        self._on_pboutput = on_pboutput
        self._on_notify_app = on_notify_app
        self._on_disconnect_async = on_disconnect_async
        self._client: mqtt.Client | None = None
        self._connected = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self) -> None:
        """Sign URL, build paho client, connect, subscribe.

        Returns once the on_connect callback has fired and subscriptions
        are in place. Raises on signing or connect failure.
        """
        self._loop = asyncio.get_running_loop()
        await self._auth.ensure_valid()

        ws_path = presigned_ws_path(
            host=self._host,
            region=self._region,
            access_key=self._auth.access_key_id,
            secret_key=self._auth.secret_access_key,
            session_token=self._auth.session_token,
        )

        client_id = f"hass-lymow-{uuid.uuid4().hex[:8]}"
        cli = mqtt.Client(
            client_id=client_id,
            transport="websockets",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        # paho's tls_set() reads default CA certs from disk synchronously,
        # which HA's event loop blocking-call detector flags. Build the SSL
        # context in an executor and attach via tls_set_context (non-blocking).
        ssl_ctx = await self._loop.run_in_executor(None, ssl.create_default_context)
        cli.tls_set_context(ssl_ctx)
        cli.ws_set_options(path=ws_path, headers={"Host": self._host})

        cli.on_connect = self._on_connect
        cli.on_subscribe = self._on_subscribe
        cli.on_disconnect = self._on_disconnect
        cli.on_message = self._on_message

        # Connect blocks; run in executor
        await self._loop.run_in_executor(None, cli.connect, self._host, 443)
        cli.loop_start()  # paho's internal thread

        self._client = cli

        # Wait until BOTH connect AND subscribe have ACK'd (set in _on_subscribe)
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            cli.loop_stop()
            cli.disconnect()
            raise ConnectionError("MQTT connect/subscribe timed out")

    async def disconnect(self) -> None:
        """Stop the loop and close the connection.

        Async because paho's loop_stop() blocks until the network thread
        joins, which can take seconds. Run in an executor so HA's event
        loop isn't stalled during integration unload.
        """
        cli = self._client
        self._client = None
        self._connected.clear()
        if cli is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, cli.loop_stop)
        cli.disconnect()

    def publish_pbinput(self, raw_pbinput: bytes) -> bool:
        """Publish a raw PbInput payload. Returns True on publish-ack success.

        Note: this is fire-and-forget at the broker level. Per arch.md §11,
        the firmware can still silently ignore the command — the coordinator's
        watchdog handles state-transition confirmation.
        """
        if not self._client or not self._connected.is_set():
            return False
        topic = _TOPIC_PBINPUT.format(thing_name=self._thing_name)
        envelope = wrap_envelope(raw_pbinput)
        info = self._client.publish(topic, envelope, qos=1)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # ── paho callbacks (run in paho's thread) ────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        _LOGGER.debug("MQTT connected rc=%s", rc)
        if rc != 0:
            _LOGGER.warning("MQTT connect rejected rc=%s", rc)
            return
        topics = [
            (_TOPIC_PBOUTPUT.format(thing_name=self._thing_name), 1),
            (_TOPIC_NOTIFY_APP.format(thing_name=self._thing_name), 1),
        ]
        client.subscribe(topics)
        # Don't signal connected yet — wait for SUBACK in _on_subscribe.

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        # paho V2 passes ReasonCode objects, not ints. Use .is_failure
        # (True for codes >= 0x80) and .value for logging.
        rejected = [rc.value for rc in granted_qos if rc.is_failure]
        if rejected:
            _LOGGER.error("MQTT subscribe rejected: %s", rejected)
            return  # Don't set _connected — caller's wait_for will time out
        _LOGGER.debug(
            "MQTT subscribed mid=%s codes=%s", mid, [rc.value for rc in granted_qos]
        )
        if self._loop:
            self._loop.call_soon_threadsafe(self._connected.set)

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        rc = args[0] if args else None
        _LOGGER.debug("MQTT disconnected rc=%s", rc)
        if self._loop:
            self._loop.call_soon_threadsafe(self._connected.clear)
        if self._on_disconnect_async and self._loop:
            self._loop.call_soon_threadsafe(self._dispatch_disconnect_async)

    def _dispatch_disconnect_async(self):
        """Run on the asyncio loop. Schedule async callback as a task; sync ones run inline."""
        cb = self._on_disconnect_async
        if cb is None:
            return
        try:
            result = cb()
        except Exception:
            _LOGGER.exception("Error in on_disconnect_async callback")
            return
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            if topic.endswith("/pboutput"):
                # Bridge to asyncio: schedule the callback
                if self._loop:
                    self._loop.call_soon_threadsafe(self._on_pboutput, bytes(msg.payload))
            elif topic.endswith("/notify-app"):
                try:
                    payload = json.loads(msg.payload.decode("utf-8"))
                    if self._loop:
                        self._loop.call_soon_threadsafe(self._on_notify_app, payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    _LOGGER.warning("Bad notify-app payload: %r", msg.payload)
        except Exception:
            _LOGGER.exception("Error in MQTT message handler")
