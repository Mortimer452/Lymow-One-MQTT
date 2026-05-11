"""Pure-function lawn-map renderer. PNG bytes out, no HA imports.

Renders a top-down map of the mower's coverage area into a PNG, suitable
for surfacing through a `camera` entity. The function takes only plain
data shapes (the ZoneCatalog, the PbPose-like pose/dock objects, a current
zone name and a task-active flag) so it's trivially unit-testable against
captured fixtures and doesn't pull HomeAssistant or asyncio into the
import graph.

Highlight semantics (arch.md §8b):

- ORANGE outline + fill on the zone the mower is physically inside
  (derived from pose-in-polygon by the caller).
- GREEN outline + fill on zones in the current task, i.e. zones with
  ``mow_order > 0`` AND ``task_active=True``. The mow_order is the
  firmware's own per-zone selection marker, written when a multi-zone
  start is dispatched; we gate on workStatus so residual mow_order values
  from a completed task don't keep glowing.
- Plain muted outline + barely-visible fill on everything else.

Orange wins over green when both apply — the "where the mower is right
now" signal is more immediate than "what's in the queue".
"""
from __future__ import annotations

import io
import math
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# Color palette — same hex values the R&E harness uses so the visual
# language is consistent for anyone who's looked at both.
_BG               = (30, 30, 46, 255)            # base canvas
_GRID             = (49, 50, 68, 255)
_POSE_OUTLINE     = (250, 179, 135, 255)         # orange — mower's zone
_POSE_FILL        = (250, 179, 135, 56)
_TASK_OUTLINE     = (166, 227, 161, 255)         # green — in current task
_TASK_FILL        = (166, 227, 161, 41)
_PLAIN_OUTLINE    = (108, 112, 134, 255)
_PLAIN_FILL       = (137, 180, 250, 13)
_DOCK_CH_OUTLINE  = (148, 226, 213, 255)         # teal — docking channel
_DOCK_CH_FILL     = (148, 226, 213, 26)
_INTER_CH_OUTLINE = (116, 199, 236, 255)         # sky — inter-zone channel
_INTER_CH_FILL    = (116, 199, 236, 20)
_DOCK_MARKER      = (249, 226, 175, 255)
_MOWER            = (250, 179, 135, 255)
_MOWER_HEADING    = (249, 226, 175, 255)
_LABEL_BG         = (30, 30, 46, 200)
_LABEL_PLAIN      = (166, 173, 200, 255)
_LABEL_TASK       = (166, 227, 161, 255)
_LABEL_POSE       = (250, 179, 135, 255)

# A modest pad around the polygon bounds so polygons don't kiss the edges
# and labels at the perimeter have room to breathe (matches the harness's
# `xmin - 1` / `xmax + 1` padding in local-frame meters).
_BOUNDS_PAD_M = 1.0
_PIXEL_MARGIN = 16


def _load_font(size: int) -> Any:
    """Best-effort font load — survives both Pillow >=10 and older."""
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, AttributeError):
        return ImageFont.load_default()


def _polygon_centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(pts)
    if n == 0:
        return (0.0, 0.0)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    return (cx, cy)


def _make_transform(
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
):
    """Build local-frame → pixel-space transform functions.

    Local-frame is mower-meters with x→east, y→north. The image is pixels
    with y→down, so the y-axis is flipped during projection. The same
    isotropic scale is used for both axes so the map isn't distorted; the
    map is centered in whichever dimension has slack.
    """
    xmin, xmax, ymin, ymax = bounds
    xrange = max(xmax - xmin, 1e-6)
    yrange = max(ymax - ymin, 1e-6)
    drawable_w = max(width - 2 * _PIXEL_MARGIN, 1)
    drawable_h = max(height - 2 * _PIXEL_MARGIN, 1)
    scale = min(drawable_w / xrange, drawable_h / yrange)
    # Offsets to center the bounding box in the drawable area.
    ox = _PIXEL_MARGIN + (drawable_w - xrange * scale) / 2.0
    oy = _PIXEL_MARGIN + (drawable_h - yrange * scale) / 2.0

    def tx(x: float) -> float:
        return ox + (x - xmin) * scale

    def ty(y: float) -> float:
        # Flip y so north points up in the rendered image.
        return height - (oy + (y - ymin) * scale)

    return tx, ty, scale


def _collect_bounds(
    catalog,
    pose,
    dock,
) -> tuple[float, float, float, float] | None:
    """Gather every (x, y) in the map data and return its bounding box."""
    xs: list[float] = []
    ys: list[float] = []
    for z in getattr(catalog, "zones", []):
        for x, y in z.polygon_points:
            xs.append(x)
            ys.append(y)
    for ch in getattr(catalog, "channels", []):
        for x, y in ch.polygon_points:
            xs.append(x)
            ys.append(y)
    if pose is not None:
        xs.append(float(pose.x))
        ys.append(float(pose.y))
    if dock is not None:
        xs.append(float(dock.x))
        ys.append(float(dock.y))
    if not xs:
        return None
    return (
        min(xs) - _BOUNDS_PAD_M,
        max(xs) + _BOUNDS_PAD_M,
        min(ys) - _BOUNDS_PAD_M,
        max(ys) + _BOUNDS_PAD_M,
    )


def _draw_dashed_segment(
    draw: ImageDraw.ImageDraw,
    p1: tuple[float, float],
    p2: tuple[float, float],
    fill: tuple[int, int, int, int],
    width: int,
    dash: int,
    gap: int,
) -> None:
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    if dist <= 0:
        return
    nx, ny = dx / dist, dy / dist
    pos = 0.0
    drawing = True
    period = dash + gap
    while pos < dist:
        seg = dash if drawing else gap
        end = min(pos + seg, dist)
        if drawing:
            draw.line(
                [(x1 + nx * pos, y1 + ny * pos), (x1 + nx * end, y1 + ny * end)],
                fill=fill,
                width=width,
            )
        pos += seg if seg else period  # safety in case of zero dash/gap
        drawing = not drawing


def _draw_dashed_polygon(
    draw: ImageDraw.ImageDraw,
    pixels: list[tuple[float, float]],
    fill: tuple[int, int, int, int],
    width: int = 1,
    dash: int = 4,
    gap: int = 3,
) -> None:
    n = len(pixels)
    for i in range(n):
        _draw_dashed_segment(draw, pixels[i], pixels[(i + 1) % n], fill, width, dash, gap)


def _text_size(font: Any, draw: ImageDraw.ImageDraw, text: str) -> tuple[int, int]:
    """Pillow API drift workaround — textbbox on modern, textsize on older."""
    if hasattr(draw, "textbbox"):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return (r - l, b - t)
    try:
        return draw.textsize(text, font=font)  # type: ignore[attr-defined]
    except AttributeError:
        # Last-resort approximation
        return (len(text) * 6, 11)


def render_map(
    catalog,
    pose: Any | None,
    dock: Any | None,
    current_zone_name: str | None,
    task_active: bool,
    width: int = 1024,
    height: int = 768,
) -> bytes | None:
    """Render a PNG of the lawn map and return its bytes.

    Returns None if the catalog has nothing renderable (no zones AND no
    pose AND no dock) — the caller should turn that into a stub image or
    a None response.

    Args:
        catalog: a ZoneCatalog (from protocol.parse_zone_catalog).
        pose: an object with float attributes x, y, theta (radians).
              None if no pose is known yet.
        dock: an object with float attributes x, y (theta ignored).
              None if no dock location is known yet.
        current_zone_name: the name of the zone the mower is physically
              inside right now, per pose-in-polygon — caller derives via
              state.derive_current_zone(). None when between zones or
              when pose is unknown.
        task_active: True iff workStatus indicates an in-progress mow
              task (caller checks against const.ACTIVE_TASK_STATUSES).
              Gates the green "current task" highlight.
        width, height: output image dimensions in pixels. HA passes None
              sometimes — caller should substitute sensible defaults.
    """
    bounds = _collect_bounds(catalog, pose, dock)
    if bounds is None:
        return None

    img = Image.new("RGBA", (width, height), _BG)
    # Polygon fills accumulate on an alpha-blended overlay so semi-transparent
    # fills compose correctly (Pillow's draw.polygon(fill=...) with alpha
    # overwrites rather than blends; one composite at the end is cheaper and
    # simpler than per-shape composites).
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    tx, ty, scale = _make_transform(bounds, width, height)

    def pxs(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(tx(p[0]), ty(p[1])) for p in pts]

    # 1) Faint grid every 5 meters of local frame.
    base_draw = ImageDraw.Draw(img)
    xmin, xmax, ymin, ymax = bounds
    gx_start = math.ceil(xmin / 5.0) * 5.0
    x = gx_start
    while x <= xmax:
        base_draw.line([(tx(x), 0), (tx(x), height)], fill=_GRID, width=1)
        x += 5.0
    gy_start = math.ceil(ymin / 5.0) * 5.0
    y = gy_start
    while y <= ymax:
        base_draw.line([(0, ty(y)), (width, ty(y))], fill=_GRID, width=1)
        y += 5.0

    # 2) Channels first so zone outlines draw on top.
    for ch in getattr(catalog, "channels", []):
        if len(ch.polygon_points) < 3:
            continue
        coords = pxs(ch.polygon_points)
        if ch.is_docking_channel:
            overlay_draw.polygon(coords, fill=_DOCK_CH_FILL)
        else:
            overlay_draw.polygon(coords, fill=_INTER_CH_FILL)

    # 3) Zone fills (deferred outlines until after composite so they're crisp).
    zone_render: list[tuple[Any, list[tuple[float, float]], str]] = []
    for z in getattr(catalog, "zones", []):
        if len(z.polygon_points) < 3:
            continue
        is_pose_zone = current_zone_name is not None and z.name == current_zone_name
        is_in_task = task_active and z.mow_order > 0
        if is_pose_zone:
            tier = "pose"
            fill = _POSE_FILL
        elif is_in_task:
            tier = "task"
            fill = _TASK_FILL
        else:
            tier = "plain"
            fill = _PLAIN_FILL
        coords = pxs(z.polygon_points)
        overlay_draw.polygon(coords, fill=fill)
        zone_render.append((z, coords, tier))

    # 4) Composite the alpha overlay onto the base in one shot.
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # 5) Channel outlines (dashed for inter-zone, solid for the docking one).
    for ch in getattr(catalog, "channels", []):
        if len(ch.polygon_points) < 3:
            continue
        coords = pxs(ch.polygon_points)
        if ch.is_docking_channel:
            draw.line(coords + [coords[0]], fill=_DOCK_CH_OUTLINE, width=2)
        else:
            _draw_dashed_polygon(draw, coords, _INTER_CH_OUTLINE, width=1)

    # 6) Zone outlines.
    for z, coords, tier in zone_render:
        if tier == "pose":
            outline = _POSE_OUTLINE
            line_w = 3
        elif tier == "task":
            outline = _TASK_OUTLINE
            line_w = 2
        else:
            outline = _PLAIN_OUTLINE
            line_w = 1
        draw.line(coords + [coords[0]], fill=outline, width=line_w)

    # 7) Zone labels — drawn before the mower marker so the marker isn't
    # obscured by its own zone's label pill.
    label_font = _load_font(12)
    for z, coords, tier in zone_render:
        if z.text_pos is not None:
            lx, ly = tx(z.text_pos[0]), ty(z.text_pos[1])
        else:
            cx, cy = _polygon_centroid(z.polygon_points)
            lx, ly = tx(cx), ty(cy)

        order_prefix = f"{z.mow_order}: " if z.mow_order > 0 else ""
        base_name = z.name or z.hash_id
        if tier == "pose":
            # ASCII ">" prefix instead of U+25B6 (▶) — Pillow's default
            # bitmap font lacks that glyph and would render tofu.
            label = f"> {order_prefix}{base_name}"
            color = _LABEL_POSE
        elif tier == "task":
            label = f"{order_prefix}{base_name}"
            color = _LABEL_TASK
        else:
            label = base_name
            color = _LABEL_PLAIN

        tw, th = _text_size(label_font, draw, label)
        # Centered around (lx, ly), with a subtle backing pill for legibility.
        x0 = lx - tw / 2 - 3
        x1 = lx + tw / 2 + 3
        y0 = ly - th / 2 - 2
        y1 = ly + th / 2 + 2
        draw.rectangle((x0, y0, x1, y1), fill=_LABEL_BG)
        draw.text((lx - tw / 2, ly - th / 2), label, fill=color, font=label_font)

    # 8) Dock marker — sits above labels (yellow square is the dock).
    if dock is not None:
        dx, dy = tx(float(dock.x)), ty(float(dock.y))
        draw.rectangle((dx - 5, dy - 5, dx + 5, dy + 5), fill=_DOCK_MARKER)

    # 9) Mower marker — always rendered last so it's visible on top of any
    # labels or fills it happens to sit on (matches the harness's draw order).
    if pose is not None:
        mx, my = tx(float(pose.x)), ty(float(pose.y))
        r = 6
        draw.ellipse((mx - r, my - r, mx + r, my + r), fill=_MOWER)
        # Pose theta is in radians, mower frame x→east / y→north. The image
        # y-axis is flipped, so the heading vector also flips its y term.
        theta = float(pose.theta)
        hx = mx + math.cos(theta) * 14
        hy = my - math.sin(theta) * 14
        draw.line([(mx, my), (hx, hy)], fill=_MOWER_HEADING, width=3)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
