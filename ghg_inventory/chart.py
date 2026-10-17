"""Static charts for the PDF report (matplotlib, PNG bytes).

Colours follow the entity, in a fixed categorical order: Scope 2 / electricity takes slot 1 (blue),
gas slot 2 (orange), then aqua, yellow, magenta for the smaller sources. Text stays in ink colours.
"""

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from ghg_inventory.calc import Totals  # noqa: E402
from ghg_inventory.scenarios import AbatementResult  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#ffffff"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8.5,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK_2,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
})


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def source_colours(source_ids: list[str]) -> dict[str, str]:
    """Fixed slot per source, in the order given (the config's source order)."""
    return {sid: SERIES[i % len(SERIES)] for i, sid in enumerate(source_ids)}


def scope_totals_chart(totals: Totals) -> bytes:
    """Two stacked bars, location- vs market-based: Scope 1 + Scope 2."""
    fig, ax = plt.subplots(figsize=(6.4, 2.1))
    labels = ["Location-based", "Market-based"]
    s1 = [totals.scope1_kg / 1000] * 2
    s2 = [totals.scope2_location_kg / 1000, totals.scope2_market_kg / 1000]
    ax.barh(labels, s2, color=SERIES[0], height=0.5, label="Scope 2 (purchased electricity)")
    ax.barh(labels, s1, left=s2, color=SERIES[1], height=0.5, label="Scope 1 (fuels, refrigerants)",
            edgecolor=SURFACE, linewidth=1.5)
    for i, (a, b) in enumerate(zip(s2, s1)):
        ax.text(a + b + 2, i, f"{a + b:,.1f} t", va="center", color=INK, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, max(a + b for a, b in zip(s2, s1)) * 1.18)
    ax.set_xlabel("t CO2e")
    ax.xaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2, frameon=False, fontsize=8)
    return _png(fig)


def source_chart(totals: Totals, colours: dict[str, str]) -> bytes:
    """Location-based emissions by source, largest first, refrigerants grouped."""
    grouped: dict[str, float] = {}
    for sid, kg in totals.by_source_kg.items():
        key = "refrigerants" if sid.startswith("refrigerant_") else sid
        grouped[key] = grouped.get(key, 0.0) + kg["location"] / 1000
    names = {**totals.source_names, "refrigerants": "Refrigerant leaks"}
    items = sorted(grouped.items(), key=lambda kv: kv[1], reverse=True)
    fig, ax = plt.subplots(figsize=(6.4, 0.42 * len(items) + 0.5))
    palette = {**colours, "refrigerants": SERIES[4]}
    ax.barh([names[k] for k, _ in items], [v for _, v in items], color=[palette[k] for k, _ in items], height=0.6)
    total = sum(v for _, v in items)
    for i, (_, v) in enumerate(items):
        ax.text(v + total * 0.01, i, f"{v:,.1f} t  ({v / total:.0%})", va="center", color=INK, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0, items[0][1] * 1.3)
    ax.set_xlabel("t CO2e, location-based")
    ax.xaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    return _png(fig)


def monthly_chart(totals: Totals, colours: dict[str, str]) -> bytes:
    """Stacked monthly bars by source (location-based). Refrigerants are annual, so left out."""
    months = [m for m in totals.by_month_kg if len(m) == 7]
    sources = [sid for sid in colours if any(totals.by_month_kg[m].get(sid) for m in months)]
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    bottom = [0.0] * len(months)
    x = range(len(months))
    for sid in sources:
        values = [totals.by_month_kg[m].get(sid, 0.0) / 1000 for m in months]
        ax.bar(x, values, bottom=bottom, color=colours[sid], width=0.7, label=totals.source_names[sid],
               edgecolor=SURFACE, linewidth=1.0)
        bottom = [b + v for b, v in zip(bottom, values)]
    ax.set_xticks(list(x), [m[5:] for m in months])
    ax.set_xlabel("Month")
    ax.set_ylabel("t CO2e")
    ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=min(4, len(sources)), frameon=False, fontsize=7.5)
    return _png(fig)


def abatement_chart(results: list[AbatementResult]) -> bytes:
    """Cost per tonne (market-based saving), cheapest first; label shows tonnes saved."""
    rows = [r for r in results if r.eur_per_t is not None]
    fig, ax = plt.subplots(figsize=(6.4, 0.42 * len(rows) + 0.6))
    values = [r.eur_per_t for r in rows]
    colours = [SERIES[2] if v < 0 else SERIES[0] for v in values]
    ax.barh([r.name for r in rows], values, color=colours, height=0.6)
    span = max(abs(v) for v in values) or 1
    for i, r in enumerate(rows):
        # Label sits right of the bar end, or right of zero for bars that go negative.
        x = max(r.eur_per_t, 0) + span * 0.02
        ax.text(x, i, f"{r.saving_market_t:,.1f} t/yr", va="center", ha="left", color=INK, fontsize=7.5)
    ax.axvline(0, color=INK_2, linewidth=0.8)
    ax.invert_yaxis()
    ax.set_xlim(min(min(values) * 1.1, -span * 0.05), max(max(values) * 1.3, span * 0.3))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_xlabel("EUR per t CO2e avoided (below zero: saves money)")
    ax.xaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    return _png(fig)
