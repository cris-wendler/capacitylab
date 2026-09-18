# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-rendered SVG small multiples: modeled utilization per option over the horizon.

One series per panel on a shared y-scale (no dual axes, no color-coded series), threshold and event
windows as quiet references, a direct label only at the peak, and a hover tooltip wired by app.js.
"""

from __future__ import annotations

import json
import math
from html import escape


def shared_y_max(outcomes: list[dict], threshold: float) -> int:
    peak = max([threshold, *(max(o["utilization_by_slot"]) for o in outcomes)])
    return int(math.ceil(max(peak, 100) / 20.0) * 20)


def utilization_panel(
    outcome: dict,
    slot_labels: list[str],
    threshold: float,
    y_max: int,
    bands: list[tuple[int, int, str]],
    width: int = 340,
    height: int = 150,
) -> str:
    values = outcome["utilization_by_slot"]
    n = len(values)
    left, right, top, bottom = 34, 12, 16, 22
    pw, ph = width - left - right, height - top - bottom

    def x(i: float) -> float:
        return left + (pw * i / max(1, n - 1))

    def y(v: float) -> float:
        return top + ph * (1 - min(v, y_max) / y_max)

    parts = [
        f'<svg class="viz" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Modeled CPU utilization for {escape(outcome["option_id"])}, peak {outcome["peak_utilization_pct"]}%" '
        f"data-values='{escape(json.dumps(values))}' data-labels='{escape(json.dumps(slot_labels))}' "
        f'data-left="{left}" data-right="{right}" data-top="{top}" data-bottom="{bottom}" data-ymax="{y_max}">'
    ]
    for start, end, label in bands:
        x0, x1 = x(start), x(max(start, end - 1))
        parts.append(f'<rect class="viz-band" x="{x0:.1f}" y="{top}" width="{max(2, x1 - x0):.1f}" height="{ph}"/>')
        parts.append(f'<text class="viz-band-label" x="{x0 + 3:.1f}" y="{top + ph - 4}">{escape(label)}</text>')
    for tick in range(0, y_max + 1, 50 if y_max > 100 else 25):
        parts.append(f'<line class="viz-grid" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>')
        parts.append(f'<text class="viz-tick" x="{left - 6}" y="{y(tick) + 3:.1f}" text-anchor="end">{tick}%</text>')
    parts.append(f'<line class="viz-threshold" x1="{left}" x2="{width - right}" y1="{y(threshold):.1f}" y2="{y(threshold):.1f}"/>')
    # Left end: the horizon starts off-peak, so the reference label does not collide with the peak label.
    parts.append(f'<text class="viz-tick" x="{left + 4}" y="{y(threshold) - 4:.1f}" text-anchor="start">'
                 f"{threshold:g}% threshold</text>")
    for i in range(0, n, max(1, n // 4)):
        parts.append(f'<text class="viz-tick" x="{x(i):.1f}" y="{height - 6}" text-anchor="middle">{escape(slot_labels[i])}</text>')
    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{x(0):.1f},{y(0):.1f} {points} {x(n - 1):.1f},{y(0):.1f}"
    parts.append(f'<polygon class="viz-area" points="{area}"/>')
    parts.append(f'<polyline class="viz-line" points="{points}"/>')
    peak_i = max(range(n), key=values.__getitem__)
    px, py = x(peak_i), y(values[peak_i])
    anchor = "end" if peak_i > n * 0.7 else "start"
    dx = -8 if anchor == "end" else 8
    parts.append(f'<circle class="viz-dot" cx="{px:.1f}" cy="{py:.1f}" r="4"/>')
    parts.append(f'<text class="viz-label" x="{px + dx:.1f}" y="{max(top + 10, py - 6):.1f}" text-anchor="{anchor}">'
                 f"peak {values[peak_i]}%</text>")
    parts.append(f'<line class="viz-crosshair" x1="0" x2="0" y1="{top}" y2="{top + ph}" visibility="hidden"/>')
    parts.append("</svg>")
    return "".join(parts)


def event_bands(scenario, horizon_labels: list[str], batch_start: str | None = None) -> list[tuple[int, int, str]]:
    h = scenario.horizon
    bands = []
    for event in scenario.events:
        if event.kind == "campaign":
            bands.append((h.slot_index(event.start), h.slot_index(event.end), "sale"))
    return bands
