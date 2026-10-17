"""Write data/activity_input.xlsx with representative figures for a small precision cutting site.

These are NOT HTWest's meter readings. They are plausible annual totals spread over the year with
realistic monthly shapes, so the tool can be shown end to end. Replace the workbook with a filled-in
template (python -m ghg_inventory make-template) once real invoices are available.

Run from the project root: python scripts/build_representative_data.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ghg_inventory.config import load_config  # noqa: E402
from ghg_inventory.excel_io import make_template  # noqa: E402
from ghg_inventory.models import ActivityData, ActivityRecord, RefrigerantRecord  # noqa: E402

ANNUAL = {
    # source_id: (annual quantity, unit, data quality, evidence prefix)
    "grid_electricity": (200_000, "kWh", 1, "REP-STROM"),     # cutting lines, compressors, extraction, HVAC
    "heating_gas": (180_000, "kWh", 1, "REP-GAS"),             # gas boiler, space heating only
    "vehicle_diesel": (2_000, "litre", 2, "REP-TANKKARTE"),     # two vans, fuel-card statements
}

# Monthly shapes. Electricity follows production, with the August shutdown (Betriebsferien).
# Gas follows heating degree days for the Rhineland.
SHAPES = {
    "grid_electricity": [1.05, 1.00, 1.03, 0.98, 0.97, 1.00, 1.03, 0.78, 1.02, 1.04, 1.06, 0.94],
    "heating_gas": [17.0, 15.0, 12.0, 8.0, 4.0, 1.5, 1.0, 1.0, 3.0, 8.0, 13.0, 16.5],
    "vehicle_diesel": [1.0, 1.0, 1.05, 1.0, 1.0, 1.0, 1.0, 0.6, 1.05, 1.05, 1.05, 0.9],
}


def monthly(total: float, shape: list[float]) -> list[float]:
    weights = [w / sum(shape) for w in shape]
    values = [round(total * w) for w in weights]
    values[-1] += round(total - sum(values))
    return values


def build() -> ActivityData:
    config, _, _ = load_config(ROOT / "config.yaml")
    year = config.reporting.year
    rows: list[ActivityRecord] = []
    for source_id, (total, unit, dq, prefix) in ANNUAL.items():
        for m, qty in enumerate(monthly(total, SHAPES[source_id]), start=1):
            rows.append(ActivityRecord(
                row_ref="", source_id=source_id, month=f"{year}-{m:02d}", quantity=qty, unit=unit,
                data_quality=dq, evidence_ref=f"{prefix}-{year}-{m:02d}", notes="representative",
            ))
    # LPG for the forklift arrives as quarterly cylinder deliveries; recorded in the delivery month.
    for m in (1, 4, 7, 10):
        rows.append(ActivityRecord(
            row_ref="", source_id="forklift_lpg", month=f"{year}-{m:02d}", quantity=250, unit="litre",
            data_quality=2, evidence_ref=f"REP-LPG-{year}-{m:02d}", notes="representative, delivery note",
        ))

    refrigerants = [
        RefrigerantRecord(row_ref="", equipment="Office split AC", refrigerant="R-32", charge_kg=3.2,
                          method="screening", annual_leak_rate=0.05, data_quality=4,
                          evidence_ref="REP-nameplate; leak rate from IPCC 2006 range for commercial AC"),
        RefrigerantRecord(row_ref="", equipment="Compressed-air refrigerant dryer", refrigerant="R-410A",
                          charge_kg=0.9, method="screening", annual_leak_rate=0.05, data_quality=4,
                          evidence_ref="REP-nameplate"),
        RefrigerantRecord(row_ref="", equipment="Server room AC", refrigerant="R-410A", charge_kg=2.5,
                          method="mass_balance", recharged_kg=0.3, data_quality=2,
                          evidence_ref="REP-F-Gas service log 2025"),
    ]
    metrics = {
        "material_processed_t": 820,
        "material_purchased_t": 900,
        "fte": 38,
        "floor_area_m2": 3200,
        "revenue_eur": 5_200_000,
    }
    return ActivityData(activities=rows, refrigerants=refrigerants, metrics=metrics)


if __name__ == "__main__":
    config, factors, base_dir = load_config(ROOT / "config.yaml")
    out = make_template(config, factors, base_dir / config.input, data=build())
    blank = make_template(config, factors, base_dir / "templates" / "activity_input_template.xlsx")
    print(f"Wrote {out.relative_to(ROOT)} and {blank.relative_to(ROOT)}")
