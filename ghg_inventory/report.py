"""PDF report. build_report_context() is pure and testable; render_pdf() lays it out with reportlab.

The prose is written once here and filled with computed numbers, so it stays true when real data
replaces the representative figures.
"""

import io
from collections import defaultdict
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from ghg_inventory.gwp import GWP_SOURCES
from ghg_inventory.inventory import InventoryResult, framework_mapping
from ghg_inventory.models import CATEGORY_LABELS, DATA_QUALITY

INK = colors.HexColor("#0b0b0b")
INK_2 = colors.HexColor("#52514e")
RULE = colors.HexColor("#d9d8d3")
HEADER_BG = colors.HexColor("#1f3a4d")
BAND = colors.HexColor("#f4f3ef")
NOTE_BG = colors.HexColor("#fff4d6")


def _t(kg: float) -> str:
    return f"{kg / 1000:,.1f}"


def _pct(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if whole else "n/a"


# ---------------------------------------------------------------- context


def _short(factor) -> str:
    """Short citation, e.g. 'DESNZ 2025', from the publisher's abbreviation in brackets if present."""
    publisher = factor.library.publisher
    short = publisher[publisher.find("(") + 1: publisher.find(")")] if "(" in publisher else publisher
    return f"{short} {factor.library.year}"


def _line_items(result: InventoryResult) -> list[dict]:
    """One row per source (refrigerants grouped), annual totals, both Scope 2 methods."""
    groups: dict[str, dict] = {}
    for line in result.lines:
        key = "refrigerants" if line.category == "fugitive" else line.source_id
        g = groups.setdefault(key, {
            "name": "Refrigerant leaks (all equipment)" if key == "refrigerants" else line.source_name,
            "scope": line.scope, "category": CATEGORY_LABELS[line.category],
            "quantity": 0.0, "unit": line.factor_unit if key != "refrigerants" else "kg released",
            "factor": "", "location_kg": 0.0, "market_kg": 0.0, "dq": set(),
        })
        if line.method in ("direct", "location"):
            g["quantity"] += line.quantity_in_factor_unit
            g["location_kg"] += line.co2e_kg
            g["dq"].add(line.data_quality)
        if line.method in ("direct", "market"):
            g["market_kg"] += line.co2e_kg
        if line.factor_id in result.factors:
            f = result.factors[line.factor_id]
            g["factor"] = _short(f)
        elif line.category == "fugitive":
            g["factor"] = f"IPCC {result.config.reporting.gwp_set} GWPs"
    for key, g in groups.items():
        g["dq"] = "-".join(str(x) for x in sorted(g["dq"])) if len(g["dq"]) > 1 else str(next(iter(g["dq"])))
        if g["scope"] == 2:
            source = result.config.source(key)
            loc = result.factors[source.location_factor]
            if source.market.uncovered_basis == "supplier_specific":
                mb = "supplier factor"
            else:
                mb = f"{_short(result.factors[source.market.residual_mix_factor])} residual mix"
            g["factor"] = f"{_short(loc)} (LB); {mb} (MB)"
    return sorted(groups.values(), key=lambda g: (g["scope"], -g["location_kg"]))


def build_report_context(result: InventoryResult) -> dict:
    cfg, t = result.config, result.totals
    total_l, total_m = t.total_location_kg, t.total_market_kg
    by_source = {sid: v["location"] for sid, v in t.by_source_kg.items()}
    refrigerant_kg = sum(v for sid, v in by_source.items() if sid.startswith("refrigerant_"))
    ranked = sorted(((sid, v) for sid, v in by_source.items() if not sid.startswith("refrigerant_")),
                    key=lambda kv: kv[1], reverse=True)
    largest_id, largest_kg = ranked[0]
    second_id, second_kg = ranked[1] if len(ranked) > 1 else (None, 0.0)
    elec = cfg.source_by_role("electricity")
    mwh = sum(e.kwh for e in result.energy) / 1000
    renewable_mwh = sum(e.renewable_kwh for e in result.energy) / 1000
    cheapest = [a for a in result.abatement if a.eur_per_t is not None and a.eur_per_t < 0]
    physical = [a for a in result.abatement if a.type != "green_tariff"]
    biggest = max(physical, key=lambda a: a.saving_location_t, default=None)
    backfires = [a for a in physical if a.saving_location_t > 0 and a.saving_market_t < 0]
    tariff = next((a for a in result.abatement if a.type == "green_tariff"), None)
    market = elec.market if elec else None

    return {
        "organisation": cfg.organisation.name,
        "site": cfg.organisation.site,
        "activity": cfg.organisation.activity,
        "year": cfg.reporting.year,
        "base_year": cfg.reporting.base_year,
        "threshold": cfg.reporting.recalculation_threshold_pct,
        "approach": cfg.reporting.consolidation_approach,
        "representative": cfg.reporting.data_status == "representative",
        "gwp_set": cfg.reporting.gwp_set,
        "gwp_source": GWP_SOURCES[cfg.reporting.gwp_set],
        "generated_at": datetime.now(timezone.utc),
        "scope1_kg": t.scope1_kg,
        "scope2_location_kg": t.scope2_location_kg,
        "scope2_market_kg": t.scope2_market_kg,
        "total_location_kg": total_l,
        "total_market_kg": total_m,
        "scope1_by_category": {CATEGORY_LABELS[k]: v for k, v in t.scope1_by_category_kg.items()},
        "biogenic_kg": t.biogenic_co2_kg,
        "largest_name": t.source_names[largest_id],
        "largest_kg": largest_kg,
        "second_name": t.source_names.get(second_id, ""),
        "second_kg": second_kg,
        "refrigerant_kg": refrigerant_kg,
        "scope2_share_location": t.scope2_location_kg / total_l if total_l else 0,
        "scope2_share_market": t.scope2_market_kg / total_m if total_m else 0,
        "draws": cfg.monte_carlo.draws,
        "location_factor": result.factors[elec.location_factor] if elec else None,
        "residual_factor": result.factors[elec.market.residual_mix_factor] if elec else None,
        "market_basis": (
            "a supplier-specific factor" if market and market.uncovered_basis == "supplier_specific"
            else "the German residual mix"),
        "eac_share": market.eac_covered_share if market else 0.0,
        "line_items": _line_items(result),
        "energy": result.energy,
        "energy_mwh": mwh,
        "renewable_mwh": renewable_mwh,
        "intensities": result.intensities,
        "uncertainty": result.uncertainty,
        "weighted_dq": t.weighted_data_quality,
        "abatement": result.abatement,
        "cheapest": cheapest,
        "biggest": biggest,
        "backfires": backfires,
        "tariff": tariff,
        "carbon_cost": result.carbon_cost,
        "scope3": result.scope3,
        "framework": framework_mapping(result),
        "factors": result.used_factors(),
        "refrigerants": result.data.refrigerants,
    }


# ---------------------------------------------------------------- prose

CO2E = "CO<sub>2</sub>e"


def _summary_text(c: dict) -> list[str]:
    paras = [
        f"In {c['year']} the site emitted <b>{_t(c['total_location_kg'])} t {CO2E}</b> across Scope 1 and 2 on a "
        f"location-based view, and <b>{_t(c['total_market_kg'])} t</b> on a market-based view. "
        f"The gap is all electricity: the grid average is one number, the contract the site buys under is another.",
        f"{escape(c['largest_name'])} is the largest source at {_t(c['largest_kg'])} t, "
        f"{_pct(c['largest_kg'], c['total_location_kg'])} of the location-based total. "
        f"{escape(c['second_name'])} comes next at {_t(c['second_kg'])} t. Everything else together is "
        f"{_pct(c['total_location_kg'] - c['largest_kg'] - c['second_kg'], c['total_location_kg'])}.",
    ]
    if c["scope2_market_kg"] > c["scope2_location_kg"]:
        paras.append(
            f"Market-based Scope 2 is higher than location-based. That is expected for a German company on a "
            f"standard tariff: the renewable power on the grid has mostly been claimed by others through "
            f"guarantees of origin, so unclaimed electricity carries {c['market_basis']}, which is dirtier than "
            f"the average. Customers who ask for a market-based figure will see the larger number.")
    return paras


METHOD_TEXT = [
    "The inventory follows the GHG Protocol Corporate Standard and the Scope 2 Guidance. Every figure is "
    "activity data multiplied by an emission factor. I calculate each gas separately (CO<sub>2</sub>, "
    "CH<sub>4</sub>, N<sub>2</sub>O and the HFCs in refrigerants), then convert to CO<sub>2</sub>e with "
    "{gwp_source}.",
    "Fuel factors come from the UK Government (DESNZ) 2025 conversion factors, the best-documented free set "
    "with a per-gas split. DESNZ publishes them in CO<sub>2</sub>e using AR5 values, so I divide back to kg "
    "of each gas before applying {gwp_set}. Gas is billed in Germany in kWh on a gross calorific value basis "
    "(Brennwert), which matches the factor used.",
    "Scope 2 is reported twice, as the Scope 2 Guidance requires. Location-based uses the {loc_publisher} "
    "factor for German electricity in {loc_year} ({loc_g:,.0f} g CO<sub>2</sub>/kWh). "
    "Market-based follows the guidance's hierarchy: kWh covered by cancelled guarantees of origin count at "
    "zero, the supplier's own disclosed factor (Stromkennzeichnung under §42 EnWG) comes next, and the "
    "residual mix for Germany ({rm_g:,.0f} g CO<sub>2</sub>/kWh, {rm_source}) covers the rest. Both "
    "electricity factors are CO<sub>2</sub> only and exclude upstream emissions.",
    "Refrigerant leaks use the GHG Protocol screening method (nameplate charge times an annual leak rate) "
    "where there are no service records, and a simplified mass balance (refrigerant topped up during the "
    "year) where there are.",
    "Biogenic CO<sub>2</sub> from the biodiesel share in diesel is reported separately, outside the scopes, "
    "as the standard asks.",
]

UNCERTAINTY_TEXT = (
    "Every activity row carries a data quality score from 1 (metered or invoiced) to 5 (rough estimate), and "
    "every factor carries its own uncertainty. I ran {draws:,} Monte Carlo draws, keeping each factor's error "
    "the same across all months that use it. The ranges below are 95% intervals. The emissions-weighted data "
    "quality score is {dq:.1f}."
)

BOUNDARY_TEXT = [
    "The boundary is the {site} site under {approach}. It covers Scope 1 (fuel burned on site and in company "
    "vehicles, plus refrigerant leaks) and Scope 2 (purchased electricity). The site buys no heat or steam.",
    "Scope 3 is outside this boundary. For a converting business it is almost certainly the largest part of "
    "the footprint: the filter media that arrive to be cut carry the emissions of their own manufacture. "
    "Inbound and outbound freight, waste, business travel and commuting are also Scope 3. I left all of them "
    "out on purpose, because Scope 1 and 2 are what the company controls directly and can measure from its "
    "own bills.",
]

BASE_YEAR_TEXT = (
    "{base_year} is the base year. I will recalculate it if a structural change (an acquisition, a new site, "
    "outsourcing), a change in method or factors, or a discovered error shifts base-year emissions by "
    "{threshold:g}% or more."
)

LIMITATIONS = [
    "This edition uses realistic figures for a site of this type, not HTWest's meter readings or invoices. "
    "The structure, factors and method are final. The numbers are placeholders until real bills go into the "
    "input workbook.",
    "It covers Scope 1 and 2 only, so it is a partial footprint by design. The Scope 3 figure above is a rough "
    "screen, not an inventory.",
    "Fuel factors are UK averages for 2025. German diesel and natural gas are close but not identical, and "
    "the UBA 2025 grid factor is an estimate that may be revised.",
    "Neither electricity factor includes CH<sub>4</sub>, N<sub>2</sub>O or upstream emissions. That is "
    "normal for Scope 2 in Germany, but it understates the full effect a little.",
    "Where there are no service records, refrigerant leak rates are defaults. Real leak rates vary a lot "
    "from one machine to the next.",
    "The abatement costs and savings use typical parameters, not quotes or measurements. The options "
    "overlap, so their savings do not add up.",
    "Real HTWest figures only go into a public repository with the company's agreement. Otherwise I would "
    "publish intensity figures or an index instead of absolute totals.",
]


# ---------------------------------------------------------------- layout


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=20,
                                leading=24, alignment=TA_LEFT, textColor=INK, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontSize=10.5, leading=14, textColor=INK_2),
        "h1": ParagraphStyle("h1", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=13.5,
                             leading=17, textColor=INK, spaceBefore=14, spaceAfter=6, keepWithNext=1),
        "h2": ParagraphStyle("h2", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10.5,
                             leading=14, textColor=INK, spaceBefore=8, spaceAfter=4, keepWithNext=1),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, leading=13.5, textColor=INK, spaceAfter=6),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=7.8, leading=10, textColor=INK_2),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=8, leading=10, textColor=INK),
        "cellb": ParagraphStyle("cellb", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8,
                                leading=10, textColor=colors.white),
        "note": ParagraphStyle("note", parent=base["Normal"], fontSize=9, leading=12.5, textColor=INK),
    }


def _table(rows: list[list], widths: list[float], styles: dict, number_cols: tuple[int, ...] = (),
           bold_last: bool = False) -> Table:
    data = []
    for r, row in enumerate(rows):
        style = styles["cellb"] if r == 0 else styles["cell"]
        data.append([c if isinstance(c, Paragraph) else Paragraph(str(c), style) for c in row])
    table = Table(data, colWidths=[w * cm for w in widths], repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for col in number_cols:
        commands.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    if bold_last:
        commands.append(("BACKGROUND", (0, -1), (-1, -1), BAND))
    table.setStyle(TableStyle(commands))
    for col in number_cols:
        for r in range(len(data)):
            data[r][col].style = ParagraphStyle(f"r{col}{r}", parent=data[r][col].style, alignment=2)
    return table


def _image(png: bytes, width_cm: float) -> Image:
    reader = ImageReader(io.BytesIO(png))
    w, h = reader.getSize()
    return Image(io.BytesIO(png), width=width_cm * cm, height=width_cm * cm * h / w)


def _footer(canvas, doc, context):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(INK_2)
    label = f"{context['organisation']} | GHG inventory {context['year']}"
    if context["representative"]:
        label += " | representative data"
    canvas.drawString(2 * cm, 1.2 * cm, label)
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
    canvas.restoreState()


def render_pdf(context: dict, charts: dict[str, bytes], output_path: str) -> None:
    c, s = context, _styles()
    doc = SimpleDocTemplate(output_path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.8 * cm, bottomMargin=2 * cm,
                            title=f"GHG inventory {c['year']}", author=c["organisation"])
    story = []
    P = Paragraph

    story.append(P(f"Greenhouse gas inventory {c['year']}", s["title"]))
    story.append(P(f"{escape(c['organisation'])}, {escape(c['site'])}. {escape(c['activity'])}. "
                   f"Scope 1 and 2, GHG Protocol Corporate Standard.", s["subtitle"]))
    story.append(Spacer(1, 8))
    if c["representative"]:
        note = Table([[P("<b>Representative data.</b> The figures in this edition are realistic estimates for a "
                         "precision cutting site of this size, not HTWest's own meter readings. The method and "
                         "factors are real; the activity numbers will be replaced with invoice data.", s["note"])]],
                     colWidths=[17 * cm])
        note.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                                  ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [note, Spacer(1, 6)]

    # Summary
    story.append(P("Summary", s["h1"]))
    for text in _summary_text(c):
        story.append(P(text, s["body"]))
    u = c["uncertainty"]
    rows = [["", f"t {CO2E}", "Share", "95% range (t)"]]
    rows.append([f"Scope 1", _t(c["scope1_kg"]), _pct(c["scope1_kg"], c["total_location_kg"]),
                 f"{u['scope1'].low_kg / 1000:,.1f} to {u['scope1'].high_kg / 1000:,.1f}"])
    for label, kg in c["scope1_by_category"].items():
        rows.append([f"&nbsp;&nbsp;&nbsp;{label}", _t(kg), "", ""])
    rows.append(["Scope 2, location-based", _t(c["scope2_location_kg"]),
                 _pct(c["scope2_location_kg"], c["total_location_kg"]),
                 f"{u['scope2_location'].low_kg / 1000:,.1f} to {u['scope2_location'].high_kg / 1000:,.1f}"])
    rows.append(["Scope 2, market-based", _t(c["scope2_market_kg"]), "",
                 f"{u['scope2_market'].low_kg / 1000:,.1f} to {u['scope2_market'].high_kg / 1000:,.1f}"])
    rows.append(["<b>Total, location-based</b>", f"<b>{_t(c['total_location_kg'])}</b>", "100%",
                 f"{u['total_location'].low_kg / 1000:,.1f} to {u['total_location'].high_kg / 1000:,.1f}"])
    rows.append(["<b>Total, market-based</b>", f"<b>{_t(c['total_market_kg'])}</b>", "",
                 f"{u['total_market'].low_kg / 1000:,.1f} to {u['total_market'].high_kg / 1000:,.1f}"])
    story.append(_table(rows, [7.5, 3, 2.5, 4], s, number_cols=(1, 2, 3)))
    story.append(P(f"Biogenic CO<sub>2</sub> from biodiesel, reported outside the scopes: "
                   f"{c['biogenic_kg'] / 1000:,.2f} t.", s["small"]))
    story.append(Spacer(1, 6))
    story.append(_image(charts["scopes"], 15.5))

    # Sources
    story.append(P("Where the emissions come from", s["h1"]))
    story.append(P(
        f"Electricity drives the cutting lines, compressors, dust extraction and HVAC, so it dominates. "
        f"Gas heats the building and follows the weather: most of it burns between November and March. "
        f"Refrigerant leaks add {_t(c['refrigerant_kg'])} t, small but not zero, and the easiest Scope 1 "
        f"source to miss.", s["body"]))
    story.append(_image(charts["sources"], 15.5))
    story.append(_image(charts["monthly"], 15.5))
    story.append(P("Monthly figures are location-based. Refrigerant leaks are annual and not shown by month.",
                   s["small"]))

    # Line items
    story.append(P("Line items", s["h1"]))
    rows = [["Source", "Scope", "Quantity", "Factor", "DQ", "t (LB)", "t (MB)"]]
    for g in c["line_items"]:
        qty = f"{g['quantity']:,.0f}" if g["quantity"] >= 100 else f"{g['quantity']:,.2f}"
        rows.append([escape(g["name"]), g["scope"], f"{qty} {escape(g['unit'])}",
                     escape(g["factor"]), g["dq"], _t(g["location_kg"]), _t(g["market_kg"])])
    rows.append(["<b>Total</b>", "", "", "", "", f"<b>{_t(c['total_location_kg'])}</b>",
                 f"<b>{_t(c['total_market_kg'])}</b>"])
    story.append(_table(rows, [4.4, 1.3, 2.9, 4.2, 0.9, 1.65, 1.65], s, number_cols=(2, 5, 6), bold_last=True))
    story.append(P("DQ is the data quality score (1 best, 5 worst). LB and MB are location- and market-based. "
                   "Every line traces back to rows in the input workbook; see inventory_lines.csv.", s["small"]))

    # Energy and intensity
    story.append(P("Energy and intensity", s["h1"]))
    rows = [["Source", "MWh", "Renewable MWh", "Renewable share"]]
    for e in c["energy"]:
        rows.append([escape(e.source_name), f"{e.kwh / 1000:,.1f}", f"{e.renewable_kwh / 1000:,.1f}",
                     _pct(e.renewable_kwh, e.kwh)])
    rows.append(["<b>Total</b>", f"<b>{c['energy_mwh']:,.1f}</b>", f"{c['renewable_mwh']:,.1f}",
                 _pct(c["renewable_mwh"], c["energy_mwh"])])
    story.append(_table(rows, [7.5, 3, 3.25, 3.25], s, number_cols=(1, 2, 3), bold_last=True))
    story.append(P("Fuels are counted at gross calorific value. The renewable share of electricity follows the "
                   "contract, as VSME B3 and ESRS E1-5 expect, not the grid average.", s["small"]))
    if c["intensities"]:
        story.append(Spacer(1, 6))
        rows = [["Intensity metric", "Location-based", "Market-based"]]
        for i in c["intensities"]:
            rows.append([escape(i.label), f"{i.location_based:,.2f}", f"{i.market_based:,.2f}"])
        story.append(_table(rows, [10, 3.5, 3.5], s, number_cols=(1, 2)))
        story.append(P("Intensity figures are what customers usually ask for, and they can be shared without "
                       "revealing absolute energy use.", s["small"]))

    # Uncertainty
    story.append(P("Uncertainty and data quality", s["h1"]))
    story.append(P(UNCERTAINTY_TEXT.format(draws=c["draws"], dq=c["weighted_dq"]), s["body"]))
    rows = [["Total", "Central (t)", "95% range (t)", "±"]]
    labels = {"scope1": "Scope 1", "scope2_location": "Scope 2, location-based",
              "scope2_market": "Scope 2, market-based", "total_location": "Total, location-based",
              "total_market": "Total, market-based"}
    for key, label in labels.items():
        r = u[key]
        rows.append([label, _t(r.central_kg), f"{r.low_kg / 1000:,.1f} to {r.high_kg / 1000:,.1f}",
                     f"{r.half_width_pct:.0f}%"])
    story.append(_table(rows, [7, 3, 4.5, 2.5], s, number_cols=(1, 2, 3)))
    rows = [["Score", "Meaning", "Activity uncertainty"]]
    for k, (label, pct) in DATA_QUALITY.items():
        rows.append([str(k), label, f"±{pct:g}%"])
    story.append(Spacer(1, 6))
    story.append(_table(rows, [1.5, 11, 4.5], s, number_cols=(2,)))

    # Method
    story.append(P("Method", s["h1"]))
    loc, rm = c["location_factor"], c["residual_factor"]
    fill = dict(gwp_source=c["gwp_source"], gwp_set=c["gwp_set"],
                loc_publisher=escape(loc.library.publisher) if loc else "", loc_year=loc.library.year if loc else "",
                loc_g=loc.entry.co2e_by_gas.get("CO2", 0) * 1000 if loc else 0,
                rm_g=rm.entry.co2e_by_gas.get("CO2", 0) * 1000 if rm else 0,
                rm_source=_short(rm) if rm else "")
    for text in METHOD_TEXT:
        story.append(P(text.format(**fill), s["body"]))

    # Boundary
    story.append(P("Boundary", s["h1"]))
    for text in BOUNDARY_TEXT:
        story.append(P(text.format(site=escape(c["site"]), approach=c["approach"]), s["body"]))
    story.append(P(BASE_YEAR_TEXT.format(base_year=c["base_year"], threshold=c["threshold"]), s["body"]))
    for screen in c["scope3"]:
        if screen.estimate_t:
            story.append(P(
                f"To put a rough number on it: {screen.quantity:,.0f} t of incoming media at the DESNZ factor "
                f"for primary paper production gives about <b>{screen.estimate_t:,.0f} t {CO2E}</b>, roughly "
                f"{screen.multiple_of_scope12_market:,.0f} times Scope 1 and 2 combined (market-based). "
                f"Paper is only a proxy for specialty filter media, and the real factor could be well above or "
                f"below it. The order of magnitude is the point: a full footprint would be mostly Scope 3.",
                s["body"]))

    # Recommendations
    story.append(P("Recommendations", s["h1"]))
    rec = []
    if c["cheapest"]:
        names = "; ".join(a.name for a in c["cheapest"])
        rec.append(f"Start with the measures that pay for themselves. On these figures that means: "
                   f"{escape(names)}. Each costs less over its life than the energy it saves.")
    if c["tariff"] and c["tariff"].saving_market_t > 0:
        a = c["tariff"]
        rec.append(f"Switching to a tariff backed by guarantees of origin would cut market-based emissions by "
                   f"about {a.saving_market_t:,.0f} t a year for roughly EUR {a.annual_cost_eur:,.0f}. It is the "
                   f"cheapest way to move the number customers see, but it changes nothing physically: the "
                   f"location-based figure stays put. Do it alongside efficiency, not instead of it. Prefer "
                   f"German or nearby GOs bought from new plants if the claim needs to hold up.")
    if c["biggest"]:
        b = c["biggest"]
        rec.append(f"Of the physical measures, '{escape(b.name)}' gives the biggest real cut. "
                   f"It saves about {b.saving_location_t:,.0f} t a year location-based "
                   f"({b.saving_market_t:,.0f} t market-based).")
    for a in c["backfires"]:
        rec.append(f"One result runs against intuition. The option '{escape(a.name)}' lowers location-based emissions by "
                   f"{a.saving_location_t:,.0f} t but raises market-based emissions by {-a.saving_market_t:,.0f} t, "
                   f"because the extra electricity is counted at the residual mix. Pair electrification with a "
                   f"cleaner electricity contract, or the number customers see goes up.")
    if c["carbon_cost"]:
        cc = c["carbon_cost"]
        rec.append(f"The national CO<sub>2</sub> price (BEHG, EUR {cc.eur_per_t:,.0f}/t in {cc.year}) already "
                   f"adds about EUR {cc.cost_eur:,.0f} a year to the gas and fuel bills. The corridor is "
                   f"EUR 55 to 65 in 2026, and fuels move into EU ETS2 from 2028 with a market price, so this "
                   f"exposure is likely to grow.")
    for text in rec:
        story.append(P(text, s["body"]))
    story.append(_image(charts["abatement"], 15.5))
    rows = [["Option", "t/yr (LB)", "t/yr (MB)", "Capex EUR", "EUR/t", "Payback"]]
    for a in c["abatement"]:
        rows.append([escape(a.name) + (f"<br/><font size=6.5 color='#52514e'>{escape(a.note)}</font>" if a.note else ""),
                     f"{a.saving_location_t:,.1f}", f"{a.saving_market_t:,.1f}", f"{a.capex_eur:,.0f}",
                     f"{a.eur_per_t:,.0f}" if a.eur_per_t is not None else "n/a",
                     f"{a.payback_years:,.1f} yr" if a.payback_years else "none"])
    story.append(_table(rows, [6.4, 1.9, 1.9, 2.2, 2.2, 2.4], s, number_cols=(1, 2, 3, 4, 5)))
    story.append(P("EUR per tonne is the annualised cost (capex over the asset life at the configured discount "
                   "rate, plus running costs, minus energy savings) divided by the market-based saving. Below "
                   "zero means the measure saves money. Options overlap and do not add up.", s["small"]))

    # Framework mapping
    story.append(KeepTogether([
        P("Where these numbers go", s["h1"]),
        P("Customers reporting under CSRD will ask suppliers for these figures, usually in the format of the "
          "voluntary SME standard (VSME). The table shows where each one belongs.", s["body"]),
        _table([["Framework", "Disclosure", "Value"]] + [[escape(a), escape(b), escape(v)] for a, b, v in c["framework"]],
               [3.8, 6.4, 6.8], s),
    ]))

    # Limitations
    story.append(P("Limitations", s["h1"]))
    for text in LIMITATIONS:
        story.append(P(text, s["body"]))

    # Sources
    story.append(P("Sources", s["h1"]))
    seen = set()
    for f in c["factors"]:
        key = (f.library.title, f.entry.reference)
        if key in seen:
            continue
        seen.add(key)
        story.append(P(f"{escape(f.library.publisher)}. <i>{escape(f.library.title)}</i>. "
                       f"{escape(f.entry.reference)}. {escape(f.library.url)}", s["small"]))
        story.append(Spacer(1, 2))
    for text in (
        "World Resources Institute and WBCSD. <i>GHG Protocol Corporate Accounting and Reporting Standard</i>, "
        "revised edition, and <i>GHG Protocol Scope 2 Guidance</i> (2015). https://ghgprotocol.org",
        f"IPCC. Global warming potentials: {c['gwp_source']}.",
        "EFRAG. <i>Voluntary Sustainability Reporting Standard for non-listed SMEs (VSME)</i>, December 2024.",
        "Brennstoffemissionshandelsgesetz (BEHG). National CO<sub>2</sub> price for heating and transport fuels.",
    ):
        story.append(P(text, s["small"]))
        story.append(Spacer(1, 2))
    story.append(Spacer(1, 6))
    story.append(P(f"Generated {c['generated_at']:%Y-%m-%d %H:%M} UTC.", s["small"]))

    doc.build(story, onFirstPage=lambda cv, d: _footer(cv, d, c), onLaterPages=lambda cv, d: _footer(cv, d, c))
