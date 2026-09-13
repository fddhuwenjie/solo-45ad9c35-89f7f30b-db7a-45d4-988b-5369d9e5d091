"""SVG rendering for annotated noise time series."""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .engine import period_overlap

WIDTH = 1100
HEIGHT = 520
MARGIN = {"left": 70, "right": 30, "top": 55, "bottom": 85}


def epoch(value: datetime) -> float:
    return value.timestamp()


def scale_x(timestamp: datetime, start: float, end: float) -> float:
    if end <= start:
        return MARGIN["left"]
    ratio = (timestamp.timestamp() - start) / (end - start)
    return MARGIN["left"] + ratio * (WIDTH - MARGIN["left"] - MARGIN["right"])


def scale_y(level: float, low: float, high: float) -> float:
    if high <= low:
        high, low = low + 1, low - 1
    ratio = (high - level) / (high - low)
    return MARGIN["top"] + ratio * (HEIGHT - MARGIN["top"] - MARGIN["bottom"])


def excluded_sample(timestamp: datetime, ranges: Iterable[tuple[datetime, datetime]]) -> bool:
    return any(
        period_overlap(timestamp, timestamp + timedelta(microseconds=1), a, b) > 0
        for a, b in ranges
    )


def polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def render_svg(request: Any, result: Any, version: int) -> str:
    samples = request.samples
    if not samples:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    start_ts = min(s.timestamp for s in samples)
    end_ts = max(s.timestamp for s in samples)
    # Extend scale to equipment/action annotations when useful.
    for period in request.equipment_periods:
        start_ts = min(start_ts, period.start)
        end_ts = max(end_ts, period.end)
    for rng in result.excluded_ranges:
        start_ts = min(start_ts, rng[0])
        end_ts = max(end_ts, rng[1])
    start_epoch, end_epoch = epoch(start_ts), epoch(end_ts)

    levels = [s.level_db for s in samples]
    low = math_floor(min(levels) - 5)
    high = math_ceil(max(levels) + 5)
    plot_bottom = HEIGHT - MARGIN["bottom"]
    plot_right = WIDTH - MARGIN["right"]

    retained_points = []
    excluded_points = []
    for sample in samples:
        point = (scale_x(sample.timestamp, start_epoch, end_epoch), scale_y(sample.level_db, low, high))
        if excluded_sample(sample.timestamp, result.excluded_ranges):
            excluded_points.append(point)
        else:
            retained_points.append(point)

    # Draw every raw sample as one continuous grey line; retained values are emphasized.
    all_points = [
        (scale_x(s.timestamp, start_epoch, end_epoch), scale_y(s.level_db, low, high))
        for s in samples
    ]
    parts: list[str] = [
        "<?xml version='1.0' encoding='UTF-8'?>",
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{WIDTH}' height='{HEIGHT}' viewBox='0 0 {WIDTH} {HEIGHT}'>",
        "<rect width='100%' height='100%' fill='white'/>",
        f"<text x='{WIDTH/2}' y='26' text-anchor='middle' font-family='Arial,sans-serif' font-size='18' font-weight='700'>"
        f"{html.escape(request.title)} — v{version} ({html.escape(request.site.point_id)})</text>",
        f"<text x='{WIDTH/2}' y='46' text-anchor='middle' font-family='Arial,sans-serif' font-size='12' fill='#555'>"
        f"Raw sequence retained; red dashed ranges show annotated exclusions</text>",
    ]

    # Night and day phase background, using site time zone if available.
    from .engine import get_zone, phase_ranges

    zone = get_zone(request.site.time_zone)
    if zone is not None and end_ts > start_ts:
        # Phase regions across the visible sample/equipment span.
        phases = phase_ranges(start_ts, end_ts, zone, request.rules.day_start_hour, request.rules.night_start_hour)
        for r_start, r_end, phase in phases:
            x1, x2 = scale_x(r_start, start_epoch, end_epoch), scale_x(r_end, start_epoch, end_epoch)
            fill = "#e9eef7" if phase == "night" else "#fffdf2"
            parts.append(
                f"<rect x='{x1:.2f}' y='{MARGIN['top']}' width='{max(0,x2-x1):.2f}' "
                f"height='{plot_bottom-MARGIN['top']}' fill='{fill}'/>"
            )

    # Grid and y labels.
    ticks = list(range(int(low), int(high) + 1, max(1, int((high-low)/8))))
    for tick in ticks:
        y = scale_y(float(tick), low, high)
        parts.append(
            f"<line x1='{MARGIN['left']}' y1='{y:.2f}' x2='{plot_right}' y2='{y:.2f}' stroke='#ddd'/>"
            f"<text x='{MARGIN['left']-10}' y='{y+4:.2f}' text-anchor='end' font-size='11' "
            f"font-family='Arial,sans-serif'>{tick}</text>"
        )
    parts.append(
        f"<text transform='translate(18 {HEIGHT/2}) rotate(-90)' text-anchor='middle' "
        f"font-family='Arial,sans-serif' font-size='13'>LAeq / sound level dB(A)</text>"
    )

    # Equipment segments near bottom.
    lane_height = 12
    equipment_lane = plot_bottom + 18
    for seg in result.segments:
        x1 = scale_x(seg.start, start_epoch, end_epoch)
        x2 = scale_x(seg.end, start_epoch, end_epoch)
        color = "#2ca02c" if seg.status == "valid" else "#d62728"
        parts.append(
            f"<rect x='{x1:.2f}' y='{equipment_lane}' width='{max(0,x2-x1):.2f}' height='{lane_height}' "
            f"fill='{color}' opacity='0.75'/>"
        )
        if x2 - x1 > 35:
            parts.append(
                f"<text x='{(x1+x2)/2:.2f}' y='{equipment_lane+lane_height+14}' text-anchor='middle' "
                f"font-size='9' font-family='Arial,sans-serif'>{html.escape(seg.equipment_id)}/{seg.phase[0]}</text>"
            )

    # Exclusion bands.
    for ex_start, ex_end in result.excluded_ranges:
        x1 = scale_x(ex_start, start_epoch, end_epoch)
        x2 = scale_x(ex_end, start_epoch, end_epoch)
        parts.append(
            f"<rect x='{x1:.2f}' y='{MARGIN['top']}' width='{max(0,x2-x1):.2f}' "
            f"height='{plot_bottom-MARGIN['top']}' fill='#ffcccc' opacity='0.28'/>"
            f"<line x1='{x1:.2f}' y1='{MARGIN['top']}' x2='{x1:.2f}' y2='{plot_bottom}' "
            f"stroke='#d62728' stroke-dasharray='4 4'/>"
            f"<line x1='{x2:.2f}' y1='{MARGIN['top']}' x2='{x2:.2f}' y2='{plot_bottom}' "
            f"stroke='#d62728' stroke-dasharray='4 4'/>"
        )

    parts.append(f"<polyline fill='none' stroke='#777' stroke-width='1.3' points='{polyline(all_points)}'/>")
    parts.append(f"<polyline fill='none' stroke='#1f77b4' stroke-width='2.2' points='{polyline(retained_points)}'/>")
    for x, y in excluded_points:
        parts.append(f"<circle cx='{x:.2f}' cy='{y:.2f}' r='3' fill='#d62728' opacity='0.85'/>")

    # Axes and time labels.
    parts.append(
        f"<line x1='{MARGIN['left']}' y1='{MARGIN['top']}' x2='{MARGIN['left']}' y2='{plot_bottom}' stroke='black'/>"
        f"<line x1='{MARGIN['left']}' y1='{plot_bottom}' x2='{plot_right}' y2='{plot_bottom}' stroke='black'/>"
    )
    label_count = 6
    for i in range(label_count + 1):
        seconds = start_epoch + (end_epoch - start_epoch) * i / label_count
        x = MARGIN["left"] + (plot_right - MARGIN["left"]) * i / label_count
        label_dt = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%m-%d\n%H:%M")
        first, second = label_dt.splitlines()
        parts.append(
            f"<line x1='{x:.2f}' y1='{plot_bottom}' x2='{x:.2f}' y2='{plot_bottom+5}' stroke='black'/>"
            f"<text x='{x:.2f}' y='{plot_bottom+20}' text-anchor='middle' font-size='10' "
            f"font-family='Arial,sans-serif'>{first}</text>"
            f"<text x='{x:.2f}' y='{plot_bottom+33}' text-anchor='middle' font-size='10' "
            f"font-family='Arial,sans-serif'>{second} UTC</text>"
        )

    parts.append(
        "<g font-family='Arial,sans-serif' font-size='12'>"
        f"<rect x='{plot_right-255}' y='{MARGIN['top']-2}' width='14' height='14' fill='#e9eef7' stroke='#ccc'/>"
        "<text x='{0}' y='{1}'>night</text>".format(plot_right - 235, MARGIN["top"] + 10) +
        f"<rect x='{plot_right-185}' y='{MARGIN['top']-2}' width='14' height='14' fill='#fffdf2' stroke='#ccc'/>"
        "<text x='{0}' y='{1}'>day</text>".format(plot_right - 165, MARGIN["top"] + 10) +
        f"<line x1='{plot_right-115}' y1='{MARGIN['top']+5}' x2='{plot_right-95}' y2='{MARGIN['top']+5}' stroke='#1f77b4' stroke-width='3'/>"
        f"<text x='{plot_right-90}' y='{MARGIN['top']+10}'>retained</text>"
        f"<circle cx='{plot_right-32}' cy='{MARGIN['top']+5}' r='4' fill='#d62728'/>"
        f"<text x='{plot_right-24}' y='{MARGIN['top']+10}'>excl.</text>"
        "</g>"
        f"<text x='{MARGIN['left']}' y='{HEIGHT-18}' font-family='Arial,sans-serif' font-size='12'>"
        f"Equipment lane: green valid, red blocked. Status: <tspan font-weight='700'>{result.status}</tspan>. "
        f"Samples raw={result.sample_index.raw_count}, effective={result.sample_index.effective_count}</text>"
        "</svg>"
    )
    return "\n".join(parts)


def math_floor(value: float) -> int:
    import math

    return int(math.floor(value / 5.0) * 5)


def math_ceil(value: float) -> int:
    import math

    return int(math.ceil(value / 5.0) * 5)
