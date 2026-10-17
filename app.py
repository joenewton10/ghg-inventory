"""Streamlit front end for the GHG inventory. Run with: python -m streamlit run app.py

The app holds no calculation logic. It edits the same inputs the CLI reads (the activity workbook and
config.yaml), runs the same engine, and writes the same output files.
"""

import copy
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

from ghg_inventory.config import ConfigError, config_from_dict, load_config, save_config
from ghg_inventory.excel_io import METRICS, make_template
from ghg_inventory.factors import UNIT_CHOICES
from ghg_inventory.gwp import GWP_SETS, GWP_SOURCES, REFRIGERANT_CHOICES, gwp
from ghg_inventory.inventory import framework_mapping, run_inventory
from ghg_inventory.models import CATEGORY_LABELS, DATA_QUALITY
from ghg_inventory.outputs import write_all_outputs
from ghg_inventory.ui_state import (
    data_from_frames, empty_frames, frames_equal, frames_from_workbook, plausibility_warnings, save_workbook,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"


def _demo_mode() -> bool:
    """A public deployment is shared by every visitor, so it must not write to disk. Demo mode keeps all
    edits in the visitor's own session and turns the save buttons into downloads. It is on automatically
    on Streamlit Community Cloud (apps run from /mount/src) and can be forced with GHG_APP_MODE=demo|local."""
    mode = os.environ.get("GHG_APP_MODE", "").strip().lower()
    if mode in ("demo", "local"):
        return mode == "demo"
    return ROOT.as_posix().startswith("/mount/src/")


DEMO = _demo_mode()

BLUE, ORANGE, AQUA, YELLOW, MAGENTA = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"
SERIES = [BLUE, ORANGE, AQUA, YELLOW, MAGENTA, "#008300", "#6250d6", "#e34948"]
INK, INK_2, GRID = "#16242E", "#52606B", "#E4E9EB"

st.set_page_config(page_title="GHG inventory", page_icon=":material/eco:", layout="wide",
                   initial_sidebar_state="auto")

st.markdown("""
<style>
  .block-container { padding-top: 2.2rem; max-width: 1240px; }
  [data-testid="stMetricValue"], [data-testid="stDataFrame"], table { font-variant-numeric: tabular-nums; }
  [data-testid="stMetricValue"] { font-size: 1.65rem; font-weight: 600; }
  [data-testid="stMetricLabel"] p { color: #52606B; font-size: 0.86rem; }
  .st-key-metric_row > div { flex: 1 1 190px !important; min-width: 190px; }
  h1 { font-size: 1.9rem !important; letter-spacing: -0.01em; }
  h2 { font-size: 1.3rem !important; margin-top: 0.6rem; }
  h3 { font-size: 1.05rem !important; }
  .lede { font-size: 1.12rem; line-height: 1.55; color: #16242E; max-width: 62ch; margin: 0.2rem 0 0.4rem; }
  .lede b { font-weight: 600; }
  .muted { color: #52606B; font-size: 0.9rem; }
  .status-note { background: #FFF4D6; border-left: 3px solid #EDA100; padding: 0.55rem 0.8rem;
                 border-radius: 3px; font-size: 0.92rem; color: #3a2f0b; margin: 0.4rem 0 1rem; max-width: 80ch; }
  .side-org { font-weight: 600; font-size: 1.02rem; line-height: 1.3; color: #16242E; }
  .side-meta { color: #52606B; font-size: 0.85rem; margin-top: 0.15rem; }
  .pill { display: inline-block; font-size: 0.78rem; font-weight: 500; padding: 0.1rem 0.5rem;
          border-radius: 999px; margin-top: 0.45rem; }
  .pill-rep { background: #FFF4D6; color: #6b4e00; }
  .pill-act { background: #DDF2E8; color: #0d5c3c; }
  .pill-dirty { background: #E7EEF7; color: #1F3A4D; }
</style>
""", unsafe_allow_html=True)


@alt.theme.register("ghg", enable=True)
def _altair_theme():
    return {"config": {
        "font": "IBM Plex Sans",
        "autosize": {"type": "fit-x", "contains": "padding"},
        "padding": {"left": 4, "right": 8, "top": 6, "bottom": 6},
        "view": {"stroke": None},
        "axis": {"labelColor": INK_2, "titleColor": INK_2, "gridColor": GRID, "domainColor": GRID,
                 "tickColor": GRID, "labelFontSize": 12, "titleFontSize": 12, "titleFontWeight": 500},
        "legend": {"labelColor": INK, "titleColor": INK_2, "labelFontSize": 12, "titleFontSize": 12,
                   "orient": "bottom", "symbolType": "square", "labelLimit": 320},
        "text": {"color": INK, "fontSize": 12},
    }}



ss = st.session_state


# ---------------------------------------------------------------- state


@st.cache_resource
def load_project():
    return load_config(CONFIG_PATH)


def _input_path(config) -> Path:
    return ROOT / config.input


def init_state(force: bool = False):
    if "frames" in ss and not force:
        return
    config, _, _ = load_project()
    try:
        frames = frames_from_workbook(_input_path(config))
        load_error = None
    except (FileNotFoundError, ValueError) as exc:
        frames, load_error = empty_frames(), str(exc)
    ss.frames = {k: v.copy() for k, v in frames.items()}
    ss.live = {k: v.copy() for k, v in frames.items()}
    ss.saved_frames = {k: v.copy() for k, v in frames.items()}
    ss.grid_version = ss.get("grid_version", 0) + 1
    ss.settings = config.model_dump(mode="json")
    ss.saved_settings = copy.deepcopy(ss.settings)
    ss.load_error = load_error
    ss.exports = None
    for key in [k for k in ss if str(k).startswith("w_")]:
        del ss[key]


def commit_grids():
    """Make grid edits the new base so they survive leaving the Data page."""
    ss.frames = {k: v.copy() for k, v in ss.live.items()}
    ss.grid_version += 1


def data_dirty() -> bool:
    return not frames_equal(ss.live, ss.saved_frames)


def settings_dirty() -> bool:
    return ss.settings != ss.saved_settings


@st.cache_resource(show_spinner=False, max_entries=24)
def compute(settings_json: str, frames: dict):
    """Validate and run. Returns (result, problems, settings_error)."""
    _, factors, _ = load_project()
    try:
        config = config_from_dict(json.loads(settings_json), factors)
    except ConfigError as exc:
        return None, [], str(exc)
    data, problems = data_from_frames(frames, config)
    if problems:
        return None, problems, None
    try:
        return run_inventory(config, factors, data), [], None
    except ValueError as exc:
        return None, [str(exc)], None


def current():
    return compute(json.dumps(ss.settings, sort_keys=True), ss.live)


# ---------------------------------------------------------------- helpers


def get_path(obj, path):
    for part in path:
        obj = obj[part]
    return obj


def set_path(obj, path, value):
    for part in path[:-1]:
        obj = obj[part]
    obj[path[-1]] = value


def bind(widget, key: str, path: tuple, *, to_ui=lambda v: v, from_ui=lambda v: v, **kwargs):
    """A widget whose value lives in ss.settings at path, and survives page changes."""
    wkey = f"w_{key}"
    if wkey not in ss:
        ss[wkey] = to_ui(get_path(ss.settings, path))
    widget(key=wkey, **kwargs)
    value = from_ui(ss[wkey])
    if get_path(ss.settings, path) != value:
        set_path(ss.settings, path, value)
    return value


def pct_in(v):
    return round(float(v) * 100, 2)


def pct_out(v):
    return round(float(v) / 100, 6)


def source_index(role: str) -> int | None:
    return next((i for i, s in enumerate(ss.settings["sources"]) if s.get("role") == role), None)


def source_colours(config) -> dict[str, str]:
    ordered = sorted(config.sources, key=lambda s: -s.scope)
    colours = {s.id: SERIES[i % len(SERIES)] for i, s in enumerate(ordered)}
    colours["refrigerants"] = MAGENTA
    return colours


def workbook_bytes(config, factors, data) -> bytes:
    """The data as an input workbook (template layout), for download."""
    with tempfile.TemporaryDirectory() as tmp:
        return make_template(config, factors, Path(tmp) / "activity_input.xlsx", data=data).read_bytes()


def config_yaml(config) -> str:
    header = ("# GHG inventory configuration, downloaded from the app.\n"
              "# Drop it next to app.py (replacing config.yaml) to use these settings locally.\n")
    return header + yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=110)


def t(kg: float) -> str:
    return f"{kg / 1000:,.1f}"


def status_note(config):
    if config.reporting.data_status == "representative":
        st.markdown('<div class="status-note">These are representative figures for a site of this type, '
                    "not HTWest's meter readings. Switch the data status to actual in Settings once real "
                    "invoices are in.</div>", unsafe_allow_html=True)


def blocked(problems, settings_error):
    if settings_error:
        st.error(f"The settings are not valid, so nothing can be calculated yet.\n\n{settings_error}")
        st.page_link(PAGES["settings"], label="Fix the settings", icon=":material/tune:")
    else:
        st.error(f"The activity data has {len(problems)} problem{'s' if len(problems) != 1 else ''}, "
                 "so the inventory can't be calculated yet.")
        for p in problems[:8]:
            st.markdown(f"- {p}")
        if len(problems) > 8:
            st.markdown(f"- and {len(problems) - 8} more")
        st.page_link(PAGES["data"], label="Fix the activity data", icon=":material/table_edit:")


# ---------------------------------------------------------------- charts


def comparison_chart(result) -> alt.Chart:
    """The headline: location- vs market-based totals as stacked bars, with the gap labelled."""
    tot = result.totals
    s1 = tot.scope1_kg / 1000
    rows, labels = [], []
    for basis, s2 in (("Location-based", tot.scope2_location_kg / 1000), ("Market-based", tot.scope2_market_kg / 1000)):
        rows.append({"basis": basis, "part": "Scope 2 (electricity)", "x": 0.0, "x2": s2, "t": s2})
        rows.append({"basis": basis, "part": "Scope 1 (fuels, refrigerants)", "x": s2, "x2": s2 + s1, "t": s1})
        labels.append({"basis": basis, "x": s2 + s1})
    gap = (tot.total_market_kg - tot.total_location_kg) / 1000
    labels[0]["label"] = f"{labels[0]['x']:,.1f} t"
    labels[1]["label"] = f"{labels[1]['x']:,.1f} t  ({gap:+,.1f} t)"
    df, lf = pd.DataFrame(rows), pd.DataFrame(labels)
    xmax = max(lf["x"]) * 1.35
    y = alt.Y("basis:N", title=None, sort=["Location-based", "Market-based"],
              scale=alt.Scale(paddingInner=0.35, paddingOuter=0.2),
              axis=alt.Axis(labelFontSize=13, labelColor=INK, labelPadding=10, ticks=False, domain=False,
                            minExtent=120))
    x_scale = alt.Scale(domain=[0, max(xmax, 1)], nice=False)
    bars = alt.Chart(df).mark_bar(stroke="#FAFBFB", strokeWidth=2).encode(
        x=alt.X("x:Q", title="t CO₂e", scale=x_scale, axis=alt.Axis(tickCount=8, grid=True)),
        x2="x2:Q", y=y,
        color=alt.Color("part:N", scale=alt.Scale(domain=["Scope 2 (electricity)", "Scope 1 (fuels, refrigerants)"],
                                                 range=[BLUE, ORANGE]), legend=alt.Legend(title=None)),
        tooltip=[alt.Tooltip("basis:N", title="Basis"), alt.Tooltip("part:N", title="Part"),
                 alt.Tooltip("t:Q", title="t CO₂e", format=",.1f")],
    )
    text = alt.Chart(lf).mark_text(align="left", dx=10, fontSize=13, fontWeight=500, color=INK).encode(
        x=alt.X("x:Q", scale=x_scale), y=y, text="label:N")
    return (bars + text).properties(height=170)


def source_chart(result, colours) -> alt.Chart:
    grouped = {}
    for sid, kg in result.totals.by_source_kg.items():
        key = "refrigerants" if sid.startswith("refrigerant_") else sid
        grouped[key] = grouped.get(key, 0.0) + kg["location"] / 1000
    names = {**result.totals.source_names, "refrigerants": "Refrigerant leaks"}
    total = sum(grouped.values()) or 1
    df = pd.DataFrame([{"id": k, "source": names[k], "t": v, "share": v / total,
                        "label": f"{v:,.1f} t  ({v / total:.0%})"} for k, v in grouped.items()])
    order = df.sort_values("t", ascending=False)["source"].tolist()
    base = alt.Chart(df).encode(y=alt.Y("source:N", sort=order, title=None,
                                        axis=alt.Axis(labelLimit=260, labelColor=INK, labelFontSize=12.5, minExtent=215)))
    bars = base.mark_bar(height=22, cornerRadiusEnd=3).encode(
        x=alt.X("t:Q", title="t CO₂e, location-based", scale=alt.Scale(domain=[0, df["t"].max() * 1.35])),
        color=alt.Color("id:N", scale=alt.Scale(domain=list(colours), range=list(colours.values())), legend=None),
        tooltip=[alt.Tooltip("source:N", title="Source"), alt.Tooltip("t:Q", title="t CO₂e", format=",.2f"),
                 alt.Tooltip("share:Q", title="Share", format=".1%")],
    )
    text = base.mark_text(align="left", dx=6).encode(x="t:Q", text="label:N")
    return (bars + text).properties(height=46 * len(df) + 20)


def monthly_chart(result, colours) -> alt.Chart:
    names = result.totals.source_names
    rows = [{"month": m, "source": names[sid], "id": sid, "t": kg / 1000}
            for m, by in result.totals.by_month_kg.items() if len(m) == 7 for sid, kg in by.items()]
    df = pd.DataFrame(rows)
    ids = [sid for sid in colours if sid in set(df["id"])]
    order = {sid: i for i, sid in enumerate(ids)}
    df["order"] = df["id"].map(order)
    return alt.Chart(df).mark_bar(stroke="#FAFBFB", strokeWidth=1.5, width=alt.RelativeBandSize(0.72)).encode(
        x=alt.X("month:O", title=None, axis=alt.Axis(labelAngle=0, labelExpr="slice(datum.value, 5)")),
        y=alt.Y("sum(t):Q", title="t CO₂e"),
        color=alt.Color("source:N", scale=alt.Scale(domain=[names[i] for i in ids],
                                                   range=[colours[i] for i in ids]),
                        legend=alt.Legend(title=None, columns=4)),
        order=alt.Order("order:Q"),
        tooltip=[alt.Tooltip("month:O", title="Month"), alt.Tooltip("source:N", title="Source"),
                 alt.Tooltip("t:Q", title="t CO₂e", format=",.2f")],
    ).properties(height=300)


def abatement_chart(results) -> alt.Chart:
    rows = [{"option": a.name, "eur_per_t": a.eur_per_t, "saving": a.saving_market_t,
             "saves_money": "Saves money" if a.eur_per_t < 0 else "Costs money",
             "label": f"{a.saving_market_t:,.1f} t/yr"} for a in results if a.eur_per_t is not None]
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["label_x"] = df["eur_per_t"].clip(lower=0)
    order = df["option"].tolist()
    lo, hi = min(df["eur_per_t"].min() * 1.1, -10), max(df["eur_per_t"].max() * 1.3, 50)
    base = alt.Chart(df).encode(y=alt.Y("option:N", sort=order, title=None,
                                        axis=alt.Axis(labelLimit=320, labelColor=INK, labelFontSize=12.5, minExtent=290)))
    bars = base.mark_bar(height=20, cornerRadius=2).encode(
        x=alt.X("eur_per_t:Q", title="EUR per t CO₂e avoided, market-based",
                scale=alt.Scale(domain=[lo, hi])),
        color=alt.Color("saves_money:N", scale=alt.Scale(domain=["Saves money", "Costs money"], range=[AQUA, BLUE]),
                        legend=alt.Legend(title=None)),
        tooltip=[alt.Tooltip("option:N", title="Option"), alt.Tooltip("eur_per_t:Q", title="EUR/t", format=",.0f"),
                 alt.Tooltip("saving:Q", title="t saved/yr (MB)", format=",.1f")],
    )
    text = base.mark_text(align="left", dx=6).encode(x="label_x:Q", text="label:N")
    rule = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=INK_2).encode(x="x:Q")
    return (bars + text + rule).properties(height=40 * len(df) + 30)


# ---------------------------------------------------------------- pages


def page_overview():
    result, problems, settings_error = current()
    cfg = result.config if result else None
    year = ss.settings["reporting"]["year"]
    st.title(f"Greenhouse gas inventory {year}")
    st.markdown(f'<p class="muted">{ss.settings["organisation"]["name"]}, {ss.settings["organisation"]["site"]}. '
                "Scope 1 and 2 under the GHG Protocol Corporate Standard.</p>", unsafe_allow_html=True)
    if result is None:
        blocked(problems, settings_error)
        return
    status_note(cfg)
    tot = result.totals
    gap = tot.total_market_kg - tot.total_location_kg
    elec = cfg.source_by_role("electricity")
    elec_share = tot.scope2_location_kg / tot.total_location_kg if tot.total_location_kg else 0
    direction = "higher" if gap >= 0 else "lower"
    st.markdown(
        f'<p class="lede">The site emitted <b>{t(tot.total_location_kg)} t CO₂e</b> on the grid-average view and '
        f'<b>{t(tot.total_market_kg)} t</b> once the electricity contract is counted. The market-based figure is '
        f'{abs(gap) / 1000:,.1f} t {direction}. Electricity is {elec_share:.0%} of the location-based total.</p>',
        unsafe_allow_html=True)
    st.altair_chart(comparison_chart(result), width="stretch", theme=None)

    u = result.uncertainty
    # A wrapping row: four tiles across on a wide screen, two by two or stacked when space is tight.
    c1 = c2 = c3 = c4 = st.container(horizontal=True, gap="small", key="metric_row")
    c1.metric("Scope 1", f"{t(tot.scope1_kg)} t", border=True,
              help=f"95% range {u['scope1'].low_kg / 1000:,.1f} to {u['scope1'].high_kg / 1000:,.1f} t")
    c2.metric("Scope 2, location-based", f"{t(tot.scope2_location_kg)} t", border=True,
              help=f"95% range {u['scope2_location'].low_kg / 1000:,.1f} to {u['scope2_location'].high_kg / 1000:,.1f} t")
    c3.metric("Scope 2, market-based", f"{t(tot.scope2_market_kg)} t", border=True,
              help=f"{elec.market.eac_covered_share:.0%} of kWh covered by guarantees of origin" if elec else None)
    c4.metric("Data quality", f"{tot.weighted_data_quality:.1f} of 5", border=True,
              help="Emissions-weighted average of the row scores. 1 is metered, 5 is a rough estimate.")

    colours = source_colours(cfg)
    tabs = st.tabs(["By source", "By month", "Energy and intensity", "Uncertainty", "Standards", "All lines"])
    with tabs[0]:
        st.altair_chart(source_chart(result, colours), width="stretch", theme=None)
        rows = []
        for cat, kg in tot.scope1_by_category_kg.items():
            rows.append({"Scope": "Scope 1", "Category": CATEGORY_LABELS[cat], "t CO₂e": kg / 1000})
        rows.append({"Scope": "Scope 2", "Category": "Purchased electricity, location-based",
                     "t CO₂e": tot.scope2_location_kg / 1000})
        rows.append({"Scope": "Scope 2", "Category": "Purchased electricity, market-based",
                     "t CO₂e": tot.scope2_market_kg / 1000})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"t CO₂e": st.column_config.NumberColumn(format="%.2f")})
        st.caption(f"Biogenic CO₂ from biodiesel, reported outside the scopes: {tot.biogenic_co2_kg / 1000:,.2f} t.")
    with tabs[1]:
        st.altair_chart(monthly_chart(result, colours), width="stretch", theme=None)
        st.caption("Location-based. Refrigerant leaks are annual figures and are not spread over months.")
    with tabs[2]:
        left, right = st.container(), st.container()
        with left:
            st.subheader("Energy use")
            energy = pd.DataFrame([{"Source": e.source_name, "MWh": e.kwh / 1000, "Renewable MWh": e.renewable_kwh / 1000,
                                    "Renewable share": e.renewable_kwh / e.kwh if e.kwh else 0} for e in result.energy])
            st.dataframe(energy, hide_index=True, width="stretch", column_config={
                "MWh": st.column_config.NumberColumn(format="%.1f"),
                "Renewable MWh": st.column_config.NumberColumn(format="%.1f"),
                "Renewable share": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
            st.caption("Fuels at gross calorific value. The renewable share of electricity follows the contract.")
        with right:
            st.subheader("Intensity")
            if result.intensities:
                st.dataframe(pd.DataFrame([{"Metric": i.label, "Location-based": i.location_based,
                                            "Market-based": i.market_based} for i in result.intensities]),
                             hide_index=True, width="stretch", column_config={
                                 "Location-based": st.column_config.NumberColumn(format="%.2f"),
                                 "Market-based": st.column_config.NumberColumn(format="%.2f")})
            else:
                st.info("Add output, headcount, floor area or revenue under Activity data, Business metrics.")
            for s3 in result.scope3:
                if s3.estimate_t:
                    st.markdown(f"**Scope 3 screen.** {s3.quantity:,.0f} t of incoming media is roughly "
                                f"{s3.estimate_t:,.0f} t CO₂e, about {s3.multiple_of_scope12_market:,.0f} times "
                                "Scope 1 and 2. Indicative only and outside the boundary.")
    with tabs[3]:
        rows = []
        labels = {"scope1": "Scope 1", "scope2_location": "Scope 2, location-based",
                  "scope2_market": "Scope 2, market-based", "total_location": "Total, location-based",
                  "total_market": "Total, market-based"}
        for key, label in labels.items():
            r = u[key]
            rows.append({"Total": label, "Central t": r.central_kg / 1000, "Low t (2.5%)": r.low_kg / 1000,
                         "High t (97.5%)": r.high_kg / 1000, "±": r.half_width_pct / 100})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "Central t": st.column_config.NumberColumn(format="%.1f"),
            "Low t (2.5%)": st.column_config.NumberColumn(format="%.1f"),
            "High t (97.5%)": st.column_config.NumberColumn(format="%.1f"),
            "±": st.column_config.NumberColumn(format="percent")})
        st.caption(f"{cfg.monte_carlo.draws:,} Monte Carlo draws. Activity uncertainty comes from each row's "
                   "data quality score, factor uncertainty from the factor library. A factor's error is shared "
                   "by every month that uses it.")
    with tabs[4]:
        st.dataframe(pd.DataFrame(framework_mapping(result), columns=["Framework", "Disclosure", "Value"]),
                     hide_index=True, width="stretch")
    with tabs[5]:
        lines = pd.DataFrame([{
            "Row": l.row_ref, "Source": l.source_name, "Scope": l.scope, "Method": l.method, "Month": l.month,
            "Quantity": l.quantity_in_factor_unit, "Unit": l.factor_unit, "Factor": l.factor_id,
            "kg CO₂e": l.co2e_kg, "DQ": l.data_quality, "Evidence": l.evidence_ref,
        } for l in result.lines])
        query = st.text_input("Filter", placeholder="Type a source, month or invoice reference",
                              label_visibility="collapsed")
        if query:
            mask = lines.astype(str).apply(lambda col: col.str.contains(query, case=False, regex=False)).any(axis=1)
            lines = lines[mask]
        st.dataframe(lines, hide_index=True, width="stretch", height=420, column_config={
            "Quantity": st.column_config.NumberColumn(format="%.2f"),
            "kg CO₂e": st.column_config.NumberColumn(format="%.1f")})


def _toolbar_data(config, factors):
    c1, c2, c3, c4 = st.columns([1.3, 1.3, 1, 1.2])
    with c1.popover("Upload workbook", icon=":material/upload:", width="stretch"):
        st.markdown("Load a filled-in input workbook. It replaces the grids below; nothing is saved until "
                    "you choose Save to workbook.")
        upload = st.file_uploader("Workbook", type=["xlsx"], label_visibility="collapsed")
        if upload is not None and st.button("Load into the grids", type="primary"):
            try:
                frames = frames_from_workbook(io.BytesIO(upload.getvalue()))
            except Exception as exc:  # noqa: BLE001 - shown to the user
                st.error(f"Could not read that file: {exc}")
            else:
                ss.frames = frames
                ss.live = {k: v.copy() for k, v in frames.items()}
                ss.grid_version += 1
                st.rerun()
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        path = make_template(config, factors, Path(tmp) / "template.xlsx")
        buffer.write(path.read_bytes())
    c2.download_button("Blank template", buffer.getvalue(), file_name="activity_input_template.xlsx",
                       icon=":material/download:", width="stretch")
    return c3, c4


def page_data():
    config_base, factors, _ = load_project()
    result, problems, settings_error = current()
    cfg = result.config if result else config_base
    st.title("Activity data")
    st.markdown('<p class="muted">One row per invoice, meter reading or fuel-card statement. Edit cells '
                "directly, add rows at the bottom, or paste a block from Excel. Results update as you type.</p>",
                unsafe_allow_html=True)
    if ss.get("load_error"):
        st.warning(f"The saved workbook could not be read, so the grids start empty. {ss.load_error}")
    revert_col, save_col = _toolbar_data(cfg, factors)

    year = ss.settings["reporting"]["year"]
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    source_ids = [s["id"] for s in ss.settings["sources"]]
    tab_a, tab_r, tab_m = st.tabs([f"Energy and fuel ({len(ss.live['activity'])})",
                                   f"Refrigerants ({len(ss.live['refrigerants'])})", "Business metrics"])
    v = ss.grid_version
    with tab_a:
        ss.live["activity"] = st.data_editor(
            ss.frames["activity"], key=f"grid_activity_{v}", num_rows="dynamic", hide_index=True,
            width="stretch", height=460,
            column_config={
                "source_id": st.column_config.SelectboxColumn("Source", options=source_ids, required=True,
                                                              help="See Factors and sources for what each covers"),
                "month": st.column_config.SelectboxColumn("Month", options=months, required=True),
                "quantity": st.column_config.NumberColumn("Quantity", min_value=0, format="%.2f", required=True),
                "unit": st.column_config.SelectboxColumn("Unit", options=list(UNIT_CHOICES), required=True),
                "data_quality": st.column_config.SelectboxColumn(
                    "Data quality", options=[1, 2, 3, 4, 5], required=True,
                    help="; ".join(f"{k}: {label}" for k, (label, _) in DATA_QUALITY.items())),
                "evidence_ref": st.column_config.TextColumn("Evidence", help="Invoice number or file name"),
                "notes": st.column_config.TextColumn("Notes"),
            })
    with tab_r:
        st.caption("One row per piece of equipment holding refrigerant. Screening needs charge and leak rate; "
                   "mass balance needs the amount topped up during the year (from the F-gas service log).")
        ss.live["refrigerants"] = st.data_editor(
            ss.frames["refrigerants"], key=f"grid_refrigerants_{v}", num_rows="dynamic", hide_index=True,
            width="stretch",
            column_config={
                "equipment": st.column_config.TextColumn("Equipment", required=True, width="medium"),
                "refrigerant": st.column_config.SelectboxColumn("Refrigerant", options=list(REFRIGERANT_CHOICES),
                                                                required=True),
                "charge_kg": st.column_config.NumberColumn("Charge kg", min_value=0, format="%.2f", required=True),
                "method": st.column_config.SelectboxColumn("Method", options=["screening", "mass_balance"],
                                                           required=True),
                "annual_leak_rate": st.column_config.NumberColumn("Leak rate", min_value=0, max_value=1,
                                                                  format="%.2f", help="0.05 means 5% a year"),
                "recharged_kg": st.column_config.NumberColumn("Topped up kg", min_value=0, format="%.2f"),
                "data_quality": st.column_config.SelectboxColumn("Data quality", options=[1, 2, 3, 4, 5],
                                                                 required=True),
                "evidence_ref": st.column_config.TextColumn("Evidence", width="medium"),
            })
    with tab_m:
        st.caption("Denominators for intensity figures and the Scope 3 screen. Leave a value empty if unknown.")
        ss.live["metrics"] = st.data_editor(
            ss.frames["metrics"], key=f"grid_metrics_{v}", hide_index=True, width="stretch",
            disabled=["metric", "unit", "notes"],
            column_config={
                "metric": st.column_config.TextColumn("Metric"),
                "value": st.column_config.NumberColumn("Value", min_value=0, format="%.2f"),
                "unit": st.column_config.TextColumn("Unit", width="small"),
                "notes": st.column_config.TextColumn("What it is", width="large"),
            })

    # Drawn after the grids so they see this run's edits.
    if revert_col.button("Revert", icon=":material/undo:", width="stretch", disabled=not data_dirty(),
                         help="Discard unsaved edits and reload the saved workbook"):
        ss.frames = {k: v.copy() for k, v in ss.saved_frames.items()}
        ss.live = {k: v.copy() for k, v in ss.saved_frames.items()}
        ss.grid_version += 1
        st.rerun()
    result, problems, settings_error = current()
    if DEMO:
        if result is not None:
            save_col.download_button("Download workbook", workbook_bytes(result.config, factors, result.data),
                                     file_name="activity_input.xlsx", type="primary", icon=":material/download:",
                                     width="stretch", help="Your edited data, in the input template layout")
        else:
            save_col.button("Download workbook", disabled=True, width="stretch", icon=":material/download:",
                            help="Fix the problems listed under Checks first")
    can_save = not DEMO and data_dirty() and not problems and not settings_error
    if not DEMO and save_col.button("Save to workbook", type="primary", icon=":material/save:", width="stretch",
                       disabled=not can_save,
                       help="Writes the grids to " + ss.settings["input"] + " (the old file is kept as a backup)"):
        data, _ = data_from_frames(ss.live, result.config)
        backup = save_workbook(result.config, factors, data, _input_path(result.config))
        ss.saved_frames = {k: x.copy() for k, x in ss.live.items()}
        commit_grids()
        st.toast(f"Saved to {ss.settings['input']}" + (f". Backup: {backup.name}" if backup else ""),
                 icon=":material/check_circle:")
        st.rerun()

    st.subheader("Checks")
    if settings_error:
        st.error("The settings are not valid; fix them under Settings.")
    elif problems:
        st.error(f"{len(problems)} problem{'s' if len(problems) != 1 else ''} to fix before the inventory "
                 "can be calculated or saved.")
        st.markdown("\n".join(f"- {p}" for p in problems[:25]) + ("\n- …" if len(problems) > 25 else ""))
    else:
        n = len(result.data.activities)
        st.success(f"All {n} activity rows and {len(result.data.refrigerants)} refrigerant rows pass the checks."
                   + (" Unsaved changes." if data_dirty() else ""), icon=":material/check:")
        for warning in plausibility_warnings(ss.live["activity"]):
            st.warning(warning, icon=":material/report:")
        totals = (ss.live["activity"].groupby(["source_id", "unit"], dropna=True)["quantity"].sum()
                  .reset_index().rename(columns={"source_id": "Source", "unit": "Unit", "quantity": "Annual total"}))
        totals["Months"] = totals["Source"].map(
            ss.live["activity"].groupby("source_id")["month"].nunique())
        st.dataframe(totals, hide_index=True, width="content", column_config={
            "Annual total": st.column_config.NumberColumn(format="%.0f")})
        st.caption("Annual totals by source. A source with fewer than 12 months may be missing bills.")


def _section(title: str, text: str):
    st.subheader(title)
    st.markdown(f'<p class="muted">{text}</p>', unsafe_allow_html=True)


def page_settings():
    st.title("Settings")
    st.markdown('<p class="muted">Changes apply straight away for this session. Save them to write '
                "config.yaml; the previous version is kept as a backup.</p>", unsafe_allow_html=True)
    bar = st.container()

    left, right = st.columns(2, gap="large")
    with left:
        _section("Organisation and reporting", "Who the inventory is for and which year it covers.")
        bind(st.text_input, "org_name", ("organisation", "name"), label="Organisation")
        bind(st.text_input, "org_site", ("organisation", "site"), label="Site")
        a, b = st.columns(2)
        with a:
            bind(st.number_input, "year", ("reporting", "year"), label="Reporting year", min_value=2000,
                 max_value=2100, step=1)
        with b:
            bind(st.number_input, "base_year", ("reporting", "base_year"), label="Base year", min_value=2000,
                 max_value=2100, step=1)
        bind(st.segmented_control, "data_status", ("reporting", "data_status"), label="Data status",
             options=["representative", "actual"], from_ui=lambda v: v or ss.settings["reporting"]["data_status"],
             format_func=lambda o: {"representative": "Representative", "actual": "Actual invoices"}[o])
        bind(st.segmented_control, "gwp", ("reporting", "gwp_set"), label="Global warming potentials",
             options=list(GWP_SETS), from_ui=lambda v: v or ss.settings["reporting"]["gwp_set"], help="; ".join(f"{k}: {v}" for k, v in GWP_SOURCES.items()))

        st.divider()
        _section("Gas meter conversion", "Only used for gas rows entered in m³. Both values are printed on "
                 "the gas bill.")
        gi = source_index("heating")
        if gi is not None:
            conv = ss.settings["sources"][gi].setdefault("unit_conversions", {}).setdefault(
                "m3", {"multiplier": 10.0, "note": ""})
            match = re.search(r"Zustandszahl ([\d.]+) x Brennwert ([\d.]+)", conv.get("note", ""))
            z0, b0 = (float(match.group(1)), float(match.group(2))) if match else (1.0, conv["multiplier"])
            a, b = st.columns(2)
            z = a.number_input("Zustandszahl", value=z0, min_value=0.5, max_value=1.2, step=0.001, format="%.4f",
                               key="w_gas_z")
            bw = b.number_input("Brennwert, kWh/m³", value=b0, min_value=8.0, max_value=13.0, step=0.01,
                                format="%.3f", key="w_gas_b")
            if abs(z * bw - conv["multiplier"]) > 1e-6:
                conv["multiplier"] = round(z * bw, 6)
                conv["note"] = f"Zustandszahl {z:g} x Brennwert {bw:g} kWh/m3, from the gas bill"
            st.caption(f"1 m³ = {conv['multiplier']:.4f} kWh")

    with right:
        ei = source_index("electricity")
        if ei is not None:
            _section("Electricity contract", "Sets the market-based Scope 2 figure. The location-based "
                     "figure always uses the grid average.")
            mpath = ("sources", ei, "market")
            bind(st.slider, "eac", mpath + ("eac_covered_share",), label="Share covered by guarantees of origin",
                 min_value=0.0, max_value=100.0, step=5.0, format="%.0f%%", to_ui=pct_in, from_ui=pct_out)
            bind(st.radio, "basis", mpath + ("uncovered_basis",), label="The rest is counted at",
                 options=["residual_mix", "supplier_specific"], horizontal=True,
                 format_func=lambda o: {"residual_mix": "German residual mix (AIB)",
                                        "supplier_specific": "Supplier's label factor"}[o])
            market = get_path(ss.settings, mpath)
            if market["uncovered_basis"] == "supplier_specific":
                if not market.get("supplier_specific"):
                    market["supplier_specific"] = {"kg_co2_per_kwh": 0.35, "renewable_share": 0.5,
                                                   "source": "Stromkennzeichnung, supplier and year"}
                spath = mpath + ("supplier_specific",)
                a, b = st.columns(2)
                with a:
                    bind(st.number_input, "sup_g", spath + ("kg_co2_per_kwh",), label="g CO₂ per kWh",
                         min_value=0.0, max_value=1500.0, step=1.0, to_ui=lambda v: round(v * 1000, 3),
                         from_ui=lambda v: round(v / 1000, 6))
                with b:
                    bind(st.number_input, "sup_re", spath + ("renewable_share",), label="Renewable share %",
                         min_value=0.0, max_value=100.0, step=1.0, to_ui=pct_in, from_ui=pct_out)
                bind(st.text_input, "sup_src", spath + ("source",), label="Source",
                     help="For example: Stromkennzeichnung 2025, Stadtwerke Bonn")
            else:
                st.caption("The residual mix is the right fallback when the supplier publishes no factor for "
                           "your tariff. It is usually much higher than the grid average.")

        st.divider()
        _section("Prices", "Used for the cost side of the scenarios and the carbon price exposure.")
        prices = ss.settings.setdefault("energy_prices_eur", {})
        units = {s["id"]: ("kWh" if s["scope"] == 2 or s.get("role") == "heating" else "litre")
                 for s in ss.settings["sources"]}
        short = {"heating": "Gas", "vehicles": "Diesel", "forklifts": "LPG", "electricity": "Electricity"}
        names = {s["id"]: short.get(s.get("role"), s["name"]) for s in ss.settings["sources"]}
        cols = st.columns(2)
        for i, sid in enumerate(prices):
            with cols[i % 2]:
                bind(st.number_input, f"price_{sid}", ("energy_prices_eur", sid),
                     label=f"{names.get(sid, sid)}, EUR/{units.get(sid, 'unit')}", min_value=0.0, step=0.01,
                     format="%.3f")
        a, b = st.columns(2)
        if ss.settings.get("carbon_price"):
            with a:
                bind(st.number_input, "co2_price", ("carbon_price", "eur_per_t_co2"), label="CO₂ price, EUR/t",
                     min_value=0.0, step=5.0, help=ss.settings["carbon_price"]["scheme"])
        with b:
            bind(st.number_input, "discount", ("discount_rate",), label="Discount rate %", min_value=0.0,
                 max_value=30.0, step=0.5, to_ui=pct_in, from_ui=pct_out)

    _, _, settings_error = current()
    if settings_error:
        st.error(settings_error)
    with bar:   # drawn last so the buttons see this run's edits
        _save_settings_bar("settings")


def _save_settings_bar(where: str):
    dirty = settings_dirty()
    a, b, _ = st.columns([1.2, 1, 3])
    if DEMO:
        _, factors, _ = load_project()
        try:
            text = config_yaml(config_from_dict(ss.settings, factors))
        except ConfigError:
            text = None
        a.download_button("Download config.yaml", text or "", file_name="config.yaml", mime="text/yaml",
                          type="primary", icon=":material/download:", width="stretch", disabled=text is None,
                          key=f"dl_settings_{where}")
    elif a.button("Save settings", type="primary", icon=":material/save:", disabled=not dirty,
                width="stretch", key=f"save_settings_{where}"):
        _, factors, _ = load_project()
        try:
            config = config_from_dict(ss.settings, factors)
        except ConfigError as exc:
            st.error(str(exc))
        else:
            backup = save_config(CONFIG_PATH, config)
            load_project.clear()
            ss.saved_settings = copy.deepcopy(ss.settings)
            st.toast("Settings saved to config.yaml" + (f". Backup: {backup.name}" if backup else ""),
                     icon=":material/check_circle:")
            st.rerun()
    if b.button("Discard changes", icon=":material/undo:", disabled=not dirty, width="stretch",
                key=f"discard_settings_{where}"):
        ss.settings = copy.deepcopy(ss.saved_settings)
        for key in [k for k in ss if str(k).startswith("w_")]:
            del ss[key]
        st.rerun()


PARAMS = {
    # key: (label, kind, min, max, step)  kind: pct | eur | num
    "compressed_air_share_of_electricity": ("Compressed air share of electricity", "pct", 0, 100, 1),
    "leak_reduction_share": ("Leakage removed", "pct", 0, 100, 1),
    "compressor_share_of_electricity": ("Compressor share of electricity", "pct", 0, 100, 1),
    "recoverable_heat_share": ("Heat that can be recovered", "pct", 0, 100, 1),
    "heating_season_utilisation": ("Share of recovered heat that is used", "pct", 0, 100, 1),
    "boiler_efficiency": ("Boiler efficiency", "pct", 50, 110, 1),
    "lighting_share_of_electricity": ("Lighting share of electricity", "pct", 0, 100, 1),
    "saving_share": ("Lighting energy saved", "pct", 0, 100, 1),
    "kwp": ("System size, kWp", "num", 0, 2000, 5),
    "yield_kwh_per_kwp": ("Yield, kWh per kWp", "num", 500, 1300, 10),
    "self_consumption_share": ("Used on site", "pct", 0, 100, 1),
    "gas_replaced_share": ("Gas replaced", "pct", 0, 100, 5),
    "seasonal_cop": ("Seasonal COP", "num", 1.5, 6.0, 0.1),
    "diesel_replaced_share": ("Diesel replaced", "pct", 0, 100, 5),
    "litres_per_100km": ("Diesel use, l/100 km", "num", 3, 20, 0.5),
    "kwh_per_100km": ("Electric use, kWh/100 km", "num", 10, 50, 1),
    "eac_covered_share": ("Share covered by guarantees of origin", "pct", 0, 100, 5),
    "premium_eur_per_kwh": ("Tariff premium, EUR/kWh", "num", 0.0, 0.1, 0.001),
    "capex_eur": ("Investment, EUR", "eur", 0, 1_000_000, 500),
    "annual_opex_eur": ("Running cost, EUR a year", "eur", 0, 100_000, 100),
    "lifetime_years": ("Lifetime, years", "num", 1, 40, 1),
}


def page_scenarios():
    st.title("Scenarios")
    st.markdown('<p class="muted">What each measure would save and what it costs per tonne. Change the '
                "assumptions below; the chart and ranking update straight away. Savings overlap, so they do not "
                "add up.</p>", unsafe_allow_html=True)
    result, problems, settings_error = current()
    if result is None:
        blocked(problems, settings_error)
        return
    bar = st.container()
    chart_slot = st.container()
    side, main = st.columns([1, 1.9], gap="large")
    with side:
        st.subheader("Assumptions")
        for i, option in enumerate(ss.settings.get("abatement", [])):
            with st.expander(option["name"]):
                for key in [k for k in option if k in PARAMS]:
                    label, kind, lo, hi, step = PARAMS[key]
                    path = ("abatement", i, key)
                    if kind == "pct":
                        bind(st.slider, f"ab_{i}_{key}", path, label=label, min_value=float(lo), max_value=float(hi),
                             step=float(step), format="%.0f%%", to_ui=pct_in, from_ui=pct_out)
                    elif kind == "eur":
                        bind(st.number_input, f"ab_{i}_{key}", path, label=label, min_value=float(lo),
                             max_value=float(hi), step=float(step), format="%.0f", to_ui=float)
                    else:
                        bind(st.number_input, f"ab_{i}_{key}", path, label=label, min_value=float(lo),
                             max_value=float(hi), step=float(step), to_ui=float,
                             format="%.3f" if step < 0.01 else ("%.1f" if step < 1 else "%.0f"))
    # Everything below is drawn after the inputs so it reflects this run's changes.
    result, problems, settings_error = current()
    with bar:
        _save_settings_bar("scenarios")
    if result is None:
        with chart_slot:
            st.error(settings_error or "; ".join(problems))
        return
    with chart_slot:
        chart = abatement_chart(result.abatement)
        if chart is not None:
            st.altair_chart(chart, width="stretch", theme=None)
    with main:
        st.subheader("Ranking")
        table = pd.DataFrame([{
            "Option": a.name, "t/yr (MB)": a.saving_market_t, "t/yr (LB)": a.saving_location_t,
            "EUR per t": a.eur_per_t, "Payback, yr": a.payback_years,
        } for a in result.abatement])
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "t/yr (MB)": st.column_config.NumberColumn(format="%.1f", help="Market-based saving"),
            "t/yr (LB)": st.column_config.NumberColumn(format="%.1f", help="Location-based saving"),
            "EUR per t": st.column_config.NumberColumn(format="%.0f"),
            "Payback, yr": st.column_config.NumberColumn(format="%.1f"),
        })
        with st.expander("Costs in detail"):
            st.dataframe(pd.DataFrame([{
                "Option": a.name, "Investment EUR": a.capex_eur, "Energy saving EUR/yr": a.annual_energy_saving_eur,
                "Net annual cost EUR": a.annual_cost_eur, "Note": a.note,
            } for a in result.abatement]), hide_index=True, width="stretch", column_config={
                "Investment EUR": st.column_config.NumberColumn(format="localized"),
                "Energy saving EUR/yr": st.column_config.NumberColumn(format="localized"),
                "Net annual cost EUR": st.column_config.NumberColumn(format="localized")})
            st.caption("Net annual cost is the investment spread over its life at the discount rate, plus running "
                       "costs and tariff premiums, minus energy savings.")
        back = [a for a in result.abatement if a.saving_location_t > 0 and a.saving_market_t < 0]
        for a in back:
            st.info(f"{a.name} cuts real emissions by {a.saving_location_t:,.1f} t but raises the market-based "
                    f"figure by {-a.saving_market_t:,.1f} t, because the extra electricity is counted at the "
                    "residual mix. Pair it with a cleaner electricity contract.", icon=":material/info:")
        if result.carbon_cost:
            cc = result.carbon_cost
            st.caption(f"The CO₂ price on fuels ({cc.eur_per_t:,.0f} EUR/t) costs about EUR {cc.cost_eur:,.0f} a "
                       f"year on {cc.covered_fossil_co2_t:,.1f} t of fossil CO₂ from gas and fuel. EU ETS2 "
                       "replaces the national price from 2028.")


def page_export():
    st.title("Export")
    st.markdown('<p class="muted">Builds the PDF report, the Excel workpaper, the line-by-line CSV and the '
                "Power BI tables from the saved inputs. The run manifest records a hash of each input file, "
                "so every number can be traced.</p>", unsafe_allow_html=True)
    if DEMO:
        st.caption("Demo: the files are built from the data and settings in this session.")
    result, problems, settings_error = current()
    if result is None:
        blocked(problems, settings_error)
        return
    unsaved = [name for name, dirty in (("activity data", data_dirty()), ("settings", settings_dirty())) if dirty]
    if unsaved and not DEMO:
        st.warning(f"You have unsaved {' and '.join(unsaved)}. Save first, so the files and the manifest "
                   "describe the same inputs.", icon=":material/edit_note:")
        if st.button("Save everything", type="primary", icon=":material/save:"):
            _, factors, _ = load_project()
            if data_dirty():
                data, _ = data_from_frames(ss.live, result.config)
                save_workbook(result.config, factors, data, _input_path(result.config))
                ss.saved_frames = {k: x.copy() for k, x in ss.live.items()}
            if settings_dirty():
                save_config(CONFIG_PATH, result.config)
                load_project.clear()
                ss.saved_settings = copy.deepcopy(ss.settings)
            st.toast("Saved", icon=":material/check_circle:")
            st.rerun()
        return

    also_save = False if DEMO else st.checkbox("Also write the files to the output folder", value=True)
    if st.button("Build report and files", type="primary", icon=":material/description:"):
        with st.spinner("Building the report"):
            config, factors, base_dir = load_project()
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                if DEMO:
                    # Build from this session's inputs, written to temp files so the manifest hashes them.
                    config_path, input_path = tmp / "config.yaml", tmp / "activity_input.xlsx"
                    config_path.write_text(config_yaml(result.config), encoding="utf-8")
                    input_path.write_bytes(workbook_bytes(result.config, factors, result.data))
                else:
                    config_path, input_path = CONFIG_PATH, _input_path(config)
                out = tmp / "out"
                paths = write_all_outputs(result, out, config_path, input_path, base_dir)
                files = {p.relative_to(out).as_posix(): p.read_bytes() for p in paths}
            if also_save:
                write_all_outputs(result, base_dir / config.output_dir, CONFIG_PATH, _input_path(config), base_dir)
            ss.exports = files
        st.toast("Report built" + (f" and saved to {ss.settings['output_dir']}/" if also_save else ""),
                 icon=":material/check_circle:")

    if ss.get("exports"):
        files = ss.exports
        year = ss.settings["reporting"]["year"]
        st.subheader("Downloads")
        rows = [
            ("ghg_inventory_report.pdf", "Report", "The document to read and share.", "application/pdf",
             f"ghg_inventory_report_{year}.pdf"),
            ("ghg_inventory.xlsx", "Excel workpaper", "Every line, factor and assumption, for an auditor.",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"ghg_inventory_{year}.xlsx"),
            ("inventory_lines.csv", "Line-by-line CSV", "One row per input row and method, with per-gas kg.",
             "text/csv", f"inventory_lines_{year}.csv"),
        ]
        for key, label, text, mime, name in rows:
            a, b = st.columns([3, 1])
            a.markdown(f"**{label}**  \n<span class='muted'>{text}</span>", unsafe_allow_html=True)
            b.download_button("Download", files[key], file_name=name, mime=mime, key=f"dl_{key}",
                              icon=":material/download:", width="stretch",
                              type="primary" if key.endswith(".pdf") else "secondary")
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            for key, content in files.items():
                if key.startswith("tables/") or key == "run_manifest.json":
                    zf.writestr(key, content)
        a, b = st.columns([3, 1])
        a.markdown("**Power BI tables and manifest**  \n<span class='muted'>A fact table per gas, three "
                   "dimension tables and the run manifest, zipped.</span>", unsafe_allow_html=True)
        b.download_button("Download", zbuf.getvalue(), file_name=f"ghg_tables_{year}.zip", mime="application/zip",
                          key="dl_zip", icon=":material/download:", width="stretch")


def page_reference():
    config, factors, _ = load_project()
    st.title("Factors and sources")
    st.markdown('<p class="muted">Read-only. Factors come from cited, versioned files in the factors folder. '
                "To use a new year's factors, add a new file and point config.yaml at it.</p>",
                unsafe_allow_html=True)
    st.subheader("Emission sources")
    st.dataframe(pd.DataFrame([{
        "Source": s.name, "ID": s.id, "Scope": s.scope, "Category": CATEGORY_LABELS[s.category],
        "Factor": s.factor or f"{s.location_factor} (LB), {s.market.residual_mix_factor} (MB residual)",
        "Unit": factors[s.factor or s.location_factor].unit,
    } for s in config.sources]), hide_index=True, width="stretch")
    st.subheader("Emission factors")
    st.dataframe(pd.DataFrame([{
        "Factor": f.factor_id, "Description": f.entry.description, "Unit": f.unit,
        "kg CO₂e per unit": sum(f.entry.co2e_by_gas.values()),
        "By gas": ", ".join(f"{g} {v:g}" for g, v in f.entry.co2e_by_gas.items()),
        "±%": f.entry.uncertainty_pct, "Source": f"{f.library.publisher}, {f.library.year}",
        "Reference": f.entry.reference, "Link": f.library.url,
    } for f in factors.values()]), hide_index=True, width="stretch", column_config={
        "kg CO₂e per unit": st.column_config.NumberColumn(format="%.5g"),
        "Link": st.column_config.LinkColumn("Link", display_text="Open")})
    st.caption("Per-gas values are as published, in CO₂e on the library's own GWP basis (AR5). The tool "
               "converts them back to kg of each gas before applying the GWP set chosen in Settings.")
    st.subheader("Refrigerant GWPs")
    st.dataframe(pd.DataFrame([{"Refrigerant": r, "AR5": gwp(r, "AR5"), "AR6": gwp(r, "AR6")}
                               for r in REFRIGERANT_CHOICES]), hide_index=True, width="content")
    st.subheader("Data quality scale")
    st.dataframe(pd.DataFrame([{"Score": k, "Meaning": label, "Activity uncertainty": f"±{u:g}%"}
                               for k, (label, u) in DATA_QUALITY.items()]), hide_index=True,
                 width="content")


GUIDE_STEPS = [
    ("settings", "Check the settings",
     "Start with who and when. Set the organisation, the reporting year and the base year. Leave the data "
     "status on Representative until real invoices are in, then switch it to Actual invoices.",
     ["Under Electricity contract, say how the power is bought. On a standard tariff, leave guarantees of "
      "origin at 0% and the rest on the German residual mix.",
      "If the bill's electricity label (Stromkennzeichnung) gives a g CO₂/kWh figure for your tariff, "
      "choose Supplier's label factor and type it in, with the supplier and year as the source.",
      "If gas bills give m³ rather than kWh, copy the Zustandszahl and Brennwert from a bill.",
      "Save settings when you are done. The previous config.yaml is kept as a backup."]),
    ("data", "Enter the energy and fuel bills",
     "One row per bill, meter reading or fuel-card statement. Pick the source, the month the energy was used, "
     "the quantity and unit as printed, a data quality score, and the invoice number as evidence.",
     ["The quickest route for a full year is Blank template: fill it in Excel, then Upload workbook.",
      "For a few changes, type straight into the grid, add rows at the bottom, or paste a block copied "
      "from Excel.",
      "Score data quality honestly: 1 for a metered or invoiced figure, up to 5 for a rough estimate. "
      "It sets the uncertainty range.",
      "If a bill covers two months, split it or enter it in the later month."]),
    ("data", "Add refrigerants and business metrics",
     "Two more tabs on the same page. They are small but easy to forget.",
     ["Refrigerants: one row per air conditioner, chiller or refrigerant air dryer. Take the refrigerant "
      "type and charge from the nameplate.",
      "Use screening with a leak rate (0.05 means 5%) if there are no service records. Use mass balance "
      "with the amount topped up during the year if the F-gas service log has it.",
      "Business metrics: material processed, material bought, staff (FTE), floor area and revenue. They "
      "drive the intensity figures and the Scope 3 screen. Leave a value empty if you don't have it."]),
    ("data", "Fix anything the checks flag, then save",
     "The Checks panel under the grids updates as you type.",
     ["Red problems stop the calculation. Each one names the tab, row and column, for example "
      "'Activity row 7, quantity'.",
      "Amber warnings don't block anything. They point at a month far above the others (usually a typo) "
      "or the same bill entered twice.",
      "Check the annual totals table. A source with fewer than 12 months may be missing a bill.",
      "Save to workbook writes data/activity_input.xlsx and keeps the old file as a backup. Until you "
      "save, edits only live in this browser session."]),
    ("overview", "Read the results",
     "The overview opens with two totals. Location-based uses the German grid average. Market-based uses the "
     "electricity contract. Customers may ask for either, so the report always shows both.",
     ["Hover over any bar for exact figures.",
      "The tabs underneath break the total down by source and by month. They also cover energy use, "
      "intensity, uncertainty ranges, where each number goes in VSME, ESRS and ISO 14064, and every "
      "calculated line.",
      "Use the filter on All lines to trace a figure back to its row and invoice."]),
    ("scenarios", "Try the reduction options",
     "Each option shows the tonnes it would save and its cost per tonne. Below zero means it saves money.",
     ["Open an option under Assumptions and change its figures. The chart and ranking update straight away.",
      "Savings overlap: heat recovery and a heat pump both cut gas, so the options don't add up.",
      "A green tariff only moves the market-based number. The note under the ranking flags any option that "
      "raises it."]),
    ("export", "Export the report",
     "When the data and settings are saved, build the files.",
     ["Report (PDF) is the document to read and share.",
      "The Excel workpaper is for an auditor: every line, factor and assumption.",
      "The CSV and the Power BI tables are for further analysis. The run manifest records a hash of every "
      "input, so each number can be traced to the files it came from."]),
]

BEFORE_YOU_START = [
    "Electricity bills or meter readings for every month of the year",
    "Gas bills in kWh (or in m³, with the Zustandszahl and Brennwert printed on them)",
    "Fuel-card statements for company vehicles",
    "Delivery notes for LPG or other fuels for forklifts and machinery",
    "Nameplates or the F-gas service log for air conditioning and refrigerant dryers",
    "The electricity label (Stromkennzeichnung) from your supplier, if it gives a factor for your tariff",
    "Tonnes of material processed and bought, staff numbers, floor area and revenue",
]


def page_guide():
    st.title("How to use this tool")
    if DEMO:
        st.info("This is the public demo, running on representative data. You can change anything, but your "
                "changes stay in this browser tab and are gone when you close it. Where the steps say Save, the "
                "demo offers a download instead, so you can keep your workbook and settings.",
                icon=":material/visibility:")
    st.markdown('<p class="lede">This guide walks you through building a Scope 1 and 2 inventory, from '
                "collecting your energy bills to exporting the finished report. Follow the steps in order the "
                "first time. In later years you mainly add the new year's bills and run it again.</p>",
                unsafe_allow_html=True)

    st.subheader("Before you start")
    st.markdown('<p class="muted">Gather these for the reporting year. Anything missing can be estimated '
                "and given a lower data quality score.</p>", unsafe_allow_html=True)
    st.markdown(bullets(BEFORE_YOU_START))
    st.divider()

    for number, (page_key, title, intro, points) in enumerate(GUIDE_STEPS, start=1):
        st.subheader(f"{number}. {title}")
        st.markdown(intro)
        st.markdown(bullets(points))
        st.page_link(PAGES[page_key], label=f"Open {PAGES[page_key].title}", icon=PAGES[page_key].icon)
        st.write("")

    st.divider()
    st.subheader("Good to know")
    st.markdown(bullets([
        "**Nothing is saved until you press Save.** Edits to data and settings last for this browser "
        "session. The sidebar shows Unsaved changes when there are any.",
        "**Every save keeps a backup.** The old workbook or config.yaml is copied with a date stamp before "
        "it is overwritten.",
        "**Emission factors are read-only here.** They come from cited files in the factors folder. When "
        "new factors are published, add a new file for that year and point config.yaml at it.",
        "**The command line gives the same result.** `python -m ghg_inventory run` reads the same files and "
        "writes the same outputs.",
        "**Keep real figures private.** Run the app on your own machine. Don't put company data on a public "
        "host without permission.",
    ]))


def bullets(items) -> str:
    return "\n".join(f"- {item}" for item in items)


# ---------------------------------------------------------------- shell

init_state()

PAGES = {
    "guide": st.Page(page_guide, title="How to use", icon=":material/help:", url_path="guide"),
    "overview": st.Page(page_overview, title="Overview", icon=":material/insights:", url_path="overview",
                        default=True),
    "data": st.Page(page_data, title="Activity data", icon=":material/table_edit:", url_path="data"),
    "settings": st.Page(page_settings, title="Settings", icon=":material/tune:", url_path="settings"),
    "scenarios": st.Page(page_scenarios, title="Scenarios", icon=":material/alt_route:", url_path="scenarios"),
    "export": st.Page(page_export, title="Export", icon=":material/ios_share:", url_path="export"),
    "reference": st.Page(page_reference, title="Factors and sources", icon=":material/menu_book:",
                         url_path="reference"),
}
nav = st.navigation({
    "Start here": [PAGES["guide"]],
    "Inventory": [PAGES["overview"]],
    "Inputs": [PAGES["data"], PAGES["settings"]],
    "Analysis": [PAGES["scenarios"]],
    "Output": [PAGES["export"], PAGES["reference"]],
})

# Grid edits live in the editor widget, which Streamlit drops when you leave the page. Fold them into
# the base frames whenever the page changes so nothing is lost.
if ss.get("last_page") == "data" and nav.url_path != "data":
    commit_grids()
ss.last_page = nav.url_path

sidebar_status = st.sidebar.empty()
nav.run()

# Filled after the page has run, so it reflects edits made in this run.
with sidebar_status.container():
    status = ss.settings["reporting"]["data_status"]
    pills = (f'<span class="pill pill-rep">Representative data</span>' if status == "representative"
             else f'<span class="pill pill-act">Actual data</span>')
    if DEMO:
        pills += ' <span class="pill pill-dirty">Demo, nothing is saved</span>'
    elif data_dirty() or settings_dirty():
        pills += ' <span class="pill pill-dirty">Unsaved changes</span>'
    st.markdown(f'<div class="side-org">{ss.settings["organisation"]["name"]}</div>'
                f'<div class="side-meta">{ss.settings["organisation"]["site"]}, '
                f'{ss.settings["reporting"]["year"]}</div>{pills}', unsafe_allow_html=True)
