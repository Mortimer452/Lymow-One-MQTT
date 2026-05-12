"""Sanity tests for map_render.

The renderer is a pure function: ZoneCatalog + pose/dock + flags → PNG bytes.
Tests focus on output shape (valid PNG, sensible dimensions, no crash on
edge cases) rather than visual fidelity — visual changes are obvious in
review and would explode pixel-diff tests anyway.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from lymow_mqtt import map_render
from lymow_mqtt.protocol import ChannelInfo, ZoneCatalog, ZoneInfo

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class FakePose:
    x: float
    y: float
    theta: float = 0.0


def _square_zone(name: str, mow_order: int = 0, offset: tuple[float, float] = (0.0, 0.0)) -> ZoneInfo:
    ox, oy = offset
    return ZoneInfo(
        hash_id=f"h_{name}",
        name=name,
        mow_order=mow_order,
        is_enabled=True,
        polygon_points=[(ox, oy), (ox + 5, oy), (ox + 5, oy + 5), (ox, oy + 5)],
    )


def _catalog_with_zones(*zones: ZoneInfo) -> ZoneCatalog:
    cat = ZoneCatalog()
    for z in zones:
        cat.zones.append(z)
        cat.zones_by_hashid[z.hash_id] = z
    return cat


class TestRenderMap:
    def test_returns_png_bytes_for_single_zone(self):
        catalog = _catalog_with_zones(_square_zone("Front"))
        out = map_render.render_map(
            catalog=catalog,
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
            width=400,
            height=300,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_empty_catalog_no_pose_no_dock_returns_none(self):
        out = map_render.render_map(
            catalog=ZoneCatalog(),
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
        )
        assert out is None

    def test_renders_with_only_pose_and_dock(self):
        # Sometimes the catalog hasn't arrived yet but pose has — the
        # caller's `available` gate prevents this in practice, but the
        # render function should still cope without crashing.
        out = map_render.render_map(
            catalog=ZoneCatalog(),
            pose=FakePose(x=1.0, y=2.0, theta=0.5),
            dock=FakePose(x=0.0, y=0.0),
            current_zone_name=None,
            task_active=False,
            width=200,
            height=200,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_task_highlight_path_does_not_crash(self):
        # Three zones, two in active task, mower physically in the second one.
        z1 = _square_zone("Front",  mow_order=1, offset=(0, 0))
        z2 = _square_zone("Side",   mow_order=2, offset=(10, 0))
        z3 = _square_zone("Back",   mow_order=0, offset=(20, 0))
        catalog = _catalog_with_zones(z1, z2, z3)
        out = map_render.render_map(
            catalog=catalog,
            pose=FakePose(x=12.5, y=2.5, theta=0.0),
            dock=FakePose(x=-2.0, y=-2.0),
            current_zone_name="Side",
            task_active=True,
            width=600,
            height=400,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_text_pos_label_override_does_not_crash(self):
        z = _square_zone("Front")
        z.text_pos = (2.0, 2.5)
        catalog = _catalog_with_zones(z)
        out = map_render.render_map(
            catalog=catalog,
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_channels_rendered_without_crash(self):
        z1 = _square_zone("A", offset=(0, 0))
        z2 = _square_zone("B", offset=(10, 0))
        catalog = _catalog_with_zones(z1, z2)
        # Tiny channel polygon between the two zones; renderer should
        # accept it and apply the dashed inter-zone styling.
        catalog.channels.append(
            ChannelInfo(
                hash_id="c1",
                zone1="h_A",
                zone2="h_B",
                is_docking_channel=False,
                polygon_points=[(5.0, 1.0), (10.0, 1.0), (10.0, 4.0), (5.0, 4.0)],
            )
        )
        catalog.channels.append(
            ChannelInfo(
                hash_id="c2",
                zone1="charging_area",
                zone2="h_A",
                is_docking_channel=True,
                polygon_points=[(-2.0, 1.0), (0.0, 1.0), (0.0, 4.0), (-2.0, 4.0)],
            )
        )
        out = map_render.render_map(
            catalog=catalog,
            pose=None,
            dock=FakePose(x=-2.5, y=2.5),
            current_zone_name=None,
            task_active=False,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    @pytest.mark.parametrize("size", [(100, 75), (1024, 768), (1920, 1080)])
    def test_honors_dimensions(self, size):
        from PIL import Image
        from io import BytesIO
        w, h = size
        out = map_render.render_map(
            catalog=_catalog_with_zones(_square_zone("Front")),
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
            width=w,
            height=h,
        )
        img = Image.open(BytesIO(out))
        assert img.size == (w, h)

    def test_polygon_with_too_few_points_is_skipped(self):
        # Degenerate zone with two points — render should skip it without
        # raising. The catalog still has rendering content (the dock), so
        # we expect a real PNG, not None.
        bad = ZoneInfo(
            hash_id="bad",
            name="Bad",
            mow_order=0,
            is_enabled=True,
            polygon_points=[(0.0, 0.0), (1.0, 1.0)],
        )
        catalog = _catalog_with_zones(bad)
        out = map_render.render_map(
            catalog=catalog,
            pose=None,
            dock=FakePose(x=0.0, y=0.0),
            current_zone_name=None,
            task_active=False,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)


class TestRenderMapWithHeatOverlay:
    """`render_map` exercising the optional ``signal_grid`` / ``cell_m``
    kwargs — when provided, the heat overlay + legend render on top of
    the zone outlines. Zone fills are always omitted (heat reads through).
    """

    def test_empty_grid_with_zones_still_renders(self):
        from lymow_mqtt import signal_grid as sg
        catalog = _catalog_with_zones(_square_zone("Front"))
        out = map_render.render_map(
            catalog=catalog,
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
            signal_grid=sg.SignalGrid(),
            cell_m=sg.CELL_M,
            width=400,
            height=300,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_populated_grid_renders_heat_overlay(self):
        from lymow_mqtt import signal_grid as sg
        catalog = _catalog_with_zones(_square_zone("Front"))
        grid = sg.SignalGrid()
        # Sprinkle samples across a few cells inside the zone — one of
        # each color bucket so every branch of _heat_color_ha is exercised.
        grid.record(1.0, 1.0, horizontal_accuracy=0.02)
        grid.record(2.0, 2.0, horizontal_accuracy=0.08)
        grid.record(3.0, 3.0, horizontal_accuracy=0.15)
        grid.record(4.0, 4.0, horizontal_accuracy=0.30)
        grid.record(4.5, 4.5, horizontal_accuracy=2.50)
        out = map_render.render_map(
            catalog=catalog,
            pose=FakePose(x=2.5, y=2.5, theta=0.0),
            dock=FakePose(x=0.0, y=0.0),
            current_zone_name="Front",
            task_active=True,
            signal_grid=grid,
            cell_m=sg.CELL_M,
            width=512,
            height=384,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_empty_everything_returns_none(self):
        from lymow_mqtt import signal_grid as sg
        out = map_render.render_map(
            catalog=ZoneCatalog(),
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
            signal_grid=sg.SignalGrid(),
            cell_m=sg.CELL_M,
        )
        assert out is None

    def test_grid_only_no_zones_still_renders(self):
        # If the user has cells from earlier mowing but the catalog hasn't
        # arrived yet on this session, the renderer should still produce
        # a heat-only image rather than refusing.
        from lymow_mqtt import signal_grid as sg
        grid = sg.SignalGrid()
        grid.record(1.0, 1.0, horizontal_accuracy=0.05)
        out = map_render.render_map(
            catalog=ZoneCatalog(),
            pose=None,
            dock=None,
            current_zone_name=None,
            task_active=False,
            signal_grid=grid,
            cell_m=sg.CELL_M,
            width=200,
            height=200,
        )
        assert out is not None
        assert out.startswith(PNG_MAGIC)
