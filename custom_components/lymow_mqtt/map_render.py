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

import colorsys
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

# Heat-map cell colors for the `signal_map` camera. Bucketed by the value
# of `horizontal_accuracy` in meters (lower = better RTK quality).
# Semi-transparent so zone outlines / channel lines remain readable on top.
#
# 10-step gradient from green (best) → yellow (mid) → red (worst), at 0.02m
# bucket width. Anything ≥0.20m saturates to the last bucket (red), so
# extreme outliers don't keep visually escalating.
_HEAT_ALPHA = 150
_HEAT_GRADIENT_STEPS = 10
_HEAT_GRADIENT_BUCKET_M = 0.02
_HEAT_GRADIENT_MAX_M = _HEAT_GRADIENT_STEPS * _HEAT_GRADIENT_BUCKET_M  # 0.20


def _gradient_step_color(i: int) -> tuple[int, int, int, int]:
    """Color for gradient bucket ``i`` (0=green, N-1=red)."""
    # Hue interpolates linearly from 120° (green) to 0° (red), passing
    # through 60° (yellow) at the midpoint. Slightly desaturated so the
    # palette sits comfortably alongside the muted Catppuccin-ish base
    # colors used elsewhere in the renderer.
    hue_deg = 120.0 * (1.0 - i / (_HEAT_GRADIENT_STEPS - 1))
    r, g, b = colorsys.hsv_to_rgb(hue_deg / 360.0, 0.75, 0.85)
    return (int(r * 255), int(g * 255), int(b * 255), _HEAT_ALPHA)


_HEAT_PALETTE_HA: tuple[tuple[int, int, int, int], ...] = tuple(
    _gradient_step_color(i) for i in range(_HEAT_GRADIENT_STEPS)
)

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


# ── Signal heat map ────────────────────────────────────────────────────────


def _heat_color_ha(value: float) -> tuple[int, int, int, int]:
    """Bucket-lookup RGBA fill for a horizontal_accuracy heat cell.

    Steps are ``_HEAT_GRADIENT_BUCKET_M`` wide starting at 0. Values at or
    above ``_HEAT_GRADIENT_MAX_M`` saturate to the last (red) step.
    """
    # Negative or nonsense values get clamped to the best bucket (defensive
    # — should not happen with PbLocalizationInfo.horizontalAccuracy, which
    # is always non-negative).
    if value <= 0.0:
        return _HEAT_PALETTE_HA[0]
    if value >= _HEAT_GRADIENT_MAX_M:
        return _HEAT_PALETTE_HA[-1]
    idx = int(value / _HEAT_GRADIENT_BUCKET_M)
    if idx >= _HEAT_GRADIENT_STEPS:
        idx = _HEAT_GRADIENT_STEPS - 1
    return _HEAT_PALETTE_HA[idx]


def _draw_heat_legend(
    img: Image.Image,
    *,
    title: str = "Horizontal accuracy (m)",
) -> None:
    """Stamp a horizontal-accuracy color-bar legend onto the rendered image.

    Draws a small legend block in the bottom-left corner: title text on top,
    a 10-cell color strip, tick labels at 0, midpoint, max. The block sits
    over the canvas background in the existing left/bottom margin so it
    rarely overlaps map content, but renders on top of anything regardless.
    """
    draw = ImageDraw.Draw(img)
    font = _load_font(11)
    img_h = img.height

    # Layout — keep the legend compact so it doesn't dominate the image.
    cell_w = 18
    cell_h = 12
    strip_w = cell_w * _HEAT_GRADIENT_STEPS
    title_h = 14
    tick_h = 14
    pad = 8

    left = pad
    bottom = img_h - pad
    strip_top = bottom - tick_h - cell_h
    title_y = strip_top - title_h

    # Background pill for legibility against either dark or busy map areas.
    box_top = title_y - 4
    box_bot = bottom
    box_right = left + strip_w + pad
    draw.rectangle((left - pad // 2, box_top, box_right, box_bot), fill=_LABEL_BG)

    # Title.
    draw.text((left, title_y), title, fill=_LABEL_PLAIN, font=font)

    # Color cells, left-to-right (best → worst).
    for i in range(_HEAT_GRADIENT_STEPS):
        x0 = left + i * cell_w
        x1 = x0 + cell_w
        # Force the legend swatch to full opacity for readability — the
        # palette entries carry the heat-cell alpha, which is intentionally
        # semi-transparent on the map but makes legend swatches washed out.
        r, g, b, _a = _HEAT_PALETTE_HA[i]
        draw.rectangle((x0, strip_top, x1, strip_top + cell_h), fill=(r, g, b, 255))

    # Tick labels — show endpoints and midpoint to keep things uncluttered.
    tick_y = strip_top + cell_h + 1
    ticks = (
        (left, "0"),
        (left + strip_w // 2, f"{_HEAT_GRADIENT_MAX_M / 2:.2f}"),
        (left + strip_w, f"{_HEAT_GRADIENT_MAX_M:.2f}+"),
    )
    for tx_px, label in ticks:
        tw, _th = _text_size(font, draw, label)
        # Center the first / last labels on their tick; left/right edges
        # would clip otherwise.
        if label == "0":
            x = tx_px
        elif label.endswith("+"):
            x = tx_px - tw
        else:
            x = tx_px - tw // 2
        draw.text((x, tick_y), label, fill=_LABEL_PLAIN, font=font)


def _collect_bounds_with_grid(
    catalog,
    pose,
    dock,
    signal_grid,
    cell_m: float,
) -> tuple[float, float, float, float] | None:
    """`_collect_bounds` plus the heat-cell extents.

    Used by the signal map so the rendered image actually contains the
    cells with samples — even if mowing has drifted beyond the catalog
    polygon bounds.
    """
    inner = _collect_bounds(catalog, pose, dock)
    xs: list[float] = [] if inner is None else [inner[0], inner[1]]
    ys: list[float] = [] if inner is None else [inner[2], inner[3]]
    for (cx, cy) in signal_grid.cells():
        xs.append(cx * cell_m)
        xs.append((cx + 1) * cell_m)
        ys.append(cy * cell_m)
        ys.append((cy + 1) * cell_m)
    if not xs:
        return None
    return (
        min(xs) - _BOUNDS_PAD_M,
        max(xs) + _BOUNDS_PAD_M,
        min(ys) - _BOUNDS_PAD_M,
        max(ys) + _BOUNDS_PAD_M,
    )


def render_signal_map(
    catalog,
    pose: Any | None,
    dock: Any | None,
    signal_grid,
    cell_m: float,
    width: int = 1024,
    height: int = 768,
) -> bytes | None:
    """Render a heat map of signal quality across the property.

    v1 colors cells by their EWMA-smoothed ``horizontal_accuracy`` value
    (lower = better RTK lock). The other three metrics (position_quality,
    wifi_signal, lte_signal) are accumulated by the coordinator but not
    yet surfaced here — future enhancement.

    Zone outlines render on top of the heat overlay so the user can
    correlate heat patterns with named zones, but zone *fills* are
    omitted (the heat is the fill).

    Args:
        catalog: a ZoneCatalog (may be empty — the function still renders
            a heat-only view if there are cells but no zones).
        pose: live mower pose (PbPose-like) or None.
        dock: dock position (PbPose-like) or None.
        signal_grid: a SignalGrid instance (from signal_grid.py). If empty
            the render falls back to the same content as `render_map` with
            no task highlighting.
        cell_m: side length of one signal-grid cell in meters. Must match
            the value the SignalGrid was built with (`signal_grid.CELL_M`).
        width, height: output dimensions in pixels.
    """
    bounds = _collect_bounds_with_grid(catalog, pose, dock, signal_grid, cell_m)
    if bounds is None:
        return None

    img = Image.new("RGBA", (width, height), _BG)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    tx, ty, scale = _make_transform(bounds, width, height)

    def pxs(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(tx(p[0]), ty(p[1])) for p in pts]

    # 1) Faint grid every 5m of local frame (sits behind everything).
    base_draw = ImageDraw.Draw(img)
    xmin, xmax, ymin, ymax = bounds
    gx = math.ceil(xmin / 5.0) * 5.0
    while gx <= xmax:
        base_draw.line([(tx(gx), 0), (tx(gx), height)], fill=_GRID, width=1)
        gx += 5.0
    gy = math.ceil(ymin / 5.0) * 5.0
    while gy <= ymax:
        base_draw.line([(0, ty(gy)), (width, ty(gy))], fill=_GRID, width=1)
        gy += 5.0

    # 2) Heat cells — one filled rect per cell that has a horizontal_accuracy
    # EWMA value. Cells with no HA samples are skipped (some cells may only
    # have wifi/lte data — invisible in v1, surfaced later).
    for (cx, cy), cell in signal_grid.cells().items():
        ha = cell.horizontal_accuracy
        if ha is None:
            continue
        color = _heat_color_ha(ha)
        wx0 = cx * cell_m
        wy0 = cy * cell_m
        # ty() flips y, so the world's bottom edge becomes the image's
        # bottom in pixel coords — i.e. larger pixel-y.
        left  = tx(wx0)
        right = tx(wx0 + cell_m)
        top   = ty(wy0 + cell_m)  # higher world-y → smaller pixel-y
        bot   = ty(wy0)
        overlay_draw.rectangle((left, top, right, bot), fill=color)

    # 3) Channel fills (very faint here — the heat is the focal point).
    for ch in getattr(catalog, "channels", []):
        if len(ch.polygon_points) < 3:
            continue
        coords = pxs(ch.polygon_points)
        fill = _DOCK_CH_FILL if ch.is_docking_channel else _INTER_CH_FILL
        overlay_draw.polygon(coords, fill=fill)

    # 4) Composite alpha overlay onto base — heat + channel fills now bake in.
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # 5) Channel outlines.
    for ch in getattr(catalog, "channels", []):
        if len(ch.polygon_points) < 3:
            continue
        coords = pxs(ch.polygon_points)
        if ch.is_docking_channel:
            draw.line(coords + [coords[0]], fill=_DOCK_CH_OUTLINE, width=2)
        else:
            _draw_dashed_polygon(draw, coords, _INTER_CH_OUTLINE, width=1)

    # 6) Zone outlines — drawn over the heat so users can see zone borders
    # against the colored cells. No fill, no highlighting.
    for z in getattr(catalog, "zones", []):
        if len(z.polygon_points) < 3:
            continue
        coords = pxs(z.polygon_points)
        draw.line(coords + [coords[0]], fill=_PLAIN_OUTLINE, width=1)

    # 7) Zone labels — small, plain, just to anchor the user.
    label_font = _load_font(12)
    for z in getattr(catalog, "zones", []):
        if len(z.polygon_points) < 3:
            continue
        if z.text_pos is not None:
            lx, ly = tx(z.text_pos[0]), ty(z.text_pos[1])
        else:
            cx_w, cy_w = _polygon_centroid(z.polygon_points)
            lx, ly = tx(cx_w), ty(cy_w)
        label = z.name or z.hash_id
        tw, th = _text_size(label_font, draw, label)
        draw.rectangle(
            (lx - tw / 2 - 3, ly - th / 2 - 2, lx + tw / 2 + 3, ly + th / 2 + 2),
            fill=_LABEL_BG,
        )
        draw.text((lx - tw / 2, ly - th / 2), label, fill=_LABEL_PLAIN, font=label_font)

    # 8) Dock marker.
    if dock is not None:
        dx, dy = tx(float(dock.x)), ty(float(dock.y))
        draw.rectangle((dx - 5, dy - 5, dx + 5, dy + 5), fill=_DOCK_MARKER)

    # 9) Mower marker — always last.
    if pose is not None:
        mx, my = tx(float(pose.x)), ty(float(pose.y))
        r = 6
        draw.ellipse((mx - r, my - r, mx + r, my + r), fill=_MOWER)
        theta = float(pose.theta)
        hx = mx + math.cos(theta) * 14
        hy = my - math.sin(theta) * 14
        draw.line([(mx, my), (hx, hy)], fill=_MOWER_HEADING, width=3)

    # 10) Heat-map legend in the bottom-left so the user can decode colors
    # without having to leave the dashboard.
    _draw_heat_legend(img)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
