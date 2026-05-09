"""Lymow lawn_mower entity — matrix-driven state, features, and dispatch.

The (work_status, robot_status, is_recharging) → (activity, button-actions)
decision lives in `state_matrix.STATE_MATRIX`. This file just wires HA's
LawnMowerEntity API to that lookup.

Why the matrix lives in its own module: it's pure data + a 10-line lookup
function, importable by unit tests without HA stubs. Adding a new edge
case is one row in `state_matrix.py`, no priority-cascade reasoning here.
"""
from __future__ import annotations

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import state_matrix
from .const import DOMAIN
from .coordinator import LymowCoordinator
from .entity_base import LymowEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: LymowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LymowMower(coord)])


class LymowMower(LymowEntity, LawnMowerEntity):
    """Lymow robot mower entity."""

    _attr_name = None  # use device name

    def __init__(self, coordinator: LymowCoordinator) -> None:
        super().__init__(coordinator, "mower")

    def _row(self) -> state_matrix.StateRow:
        """Look up the current matrix row from the latest robotInfo.

        Returns DEFAULT_ROW if robotInfo isn't present yet (fresh integration
        startup before any broadcast). DEFAULT_ROW has activity=None and no
        actions, so HA renders "Unknown" with no buttons until state arrives.
        """
        ri = self.coordinator.state_dict.get("robotInfo")
        if ri is None:
            return state_matrix.DEFAULT_ROW
        return state_matrix.lookup(
            work_status=ri.workStatus,
            robot_status=ri.robotStatus,
            is_recharging=bool(getattr(ri, "isRecharging", False)),
        )

    @property
    def activity(self) -> LawnMowerActivity | None:
        val = self._row().activity
        return LawnMowerActivity(val) if val is not None else None

    @property
    def supported_features(self) -> LawnMowerEntityFeature:
        return state_matrix.features_for(self._row())

    async def async_start_mowing(self) -> None:
        await self.coordinator.cmd_button_press("start_mowing")

    async def async_pause(self) -> None:
        await self.coordinator.cmd_button_press("pause")

    async def async_dock(self) -> None:
        """Dock and KEEP task progress (the safer default).

        Use the lymow_mqtt.dock_cancel_task service for the destructive
        variant that abandons the task.
        """
        await self.coordinator.cmd_button_press("dock")
