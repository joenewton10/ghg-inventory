import argparse
import sys
from pathlib import Path

from ghg_inventory.calc import CalculationError
from ghg_inventory.chart import abatement_chart, monthly_chart, scope_totals_chart, source_chart, source_colours
from ghg_inventory.config import ConfigError, load_config
from ghg_inventory.excel_io import InputError, make_template, read_input
from ghg_inventory.inventory import run_inventory
from ghg_inventory.outputs import write_lines_csv, write_manifest, write_tidy_tables, write_workbook
from ghg_inventory.report import build_report_context, render_pdf


def _run(args) -> int:
    config_path = Path(args.config)
    config, factors, base_dir = load_config(config_path)
    input_path = Path(args.input) if args.input else base_dir / config.input
    data = read_input(input_path, config)
    result = run_inventory(config, factors, data)

    out = Path(args.output_dir) if args.output_dir else base_dir / config.output_dir
    out.mkdir(parents=True, exist_ok=True)
    # Scope 2 first so electricity keeps slot 1 (blue) in every chart, matching the scope chart.
    colours = source_colours([s.id for s in sorted(config.sources, key=lambda s: -s.scope)])
    charts = {
        "scopes": scope_totals_chart(result.totals),
        "sources": source_chart(result.totals, colours),
        "monthly": monthly_chart(result.totals, colours),
        "abatement": abatement_chart(result.abatement),
    }
    written = [
        write_lines_csv(result, out / "inventory_lines.csv"),
        write_workbook(result, out / "ghg_inventory.xlsx"),
        *write_tidy_tables(result, out / "tables"),
    ]
    report_path = out / "ghg_inventory_report.pdf"
    render_pdf(build_report_context(result), charts, str(report_path))
    written.append(report_path)
    manifest = write_manifest(result, config_path.resolve(), input_path.resolve(), base_dir, written,
                              out / "run_manifest.json")

    t = result.totals
    status = " (REPRESENTATIVE DATA)" if config.reporting.data_status == "representative" else ""
    print(f"{config.organisation.name}, {config.reporting.year}{status}, GWP {config.reporting.gwp_set}")
    print(f"  Scope 1                  {t.scope1_kg / 1000:10,.1f} t CO2e")
    print(f"  Scope 2 location-based   {t.scope2_location_kg / 1000:10,.1f} t CO2e")
    print(f"  Scope 2 market-based     {t.scope2_market_kg / 1000:10,.1f} t CO2e")
    print(f"  Total location-based     {t.total_location_kg / 1000:10,.1f} t CO2e")
    print(f"  Total market-based       {t.total_market_kg / 1000:10,.1f} t CO2e")
    print(f"Wrote {len(written) + 1} files to {out} (manifest: {manifest.name})")
    return 0


def _template(args) -> int:
    config, factors, base_dir = load_config(Path(args.config))
    path = make_template(config, factors, Path(args.out) if args.out else base_dir / "templates" / "activity_input_template.xlsx")
    print(f"Blank input template written to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ghg-inventory", description="GHG Protocol Scope 1 and 2 inventory.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Calculate the inventory and write CSV, Excel, PDF and tidy tables")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--input", help="Override the input workbook (or a folder of CSVs)")
    run.add_argument("--output-dir", help="Override the output folder")
    run.set_defaults(func=_run)
    tpl = sub.add_parser("make-template", help="Write a blank Excel input template")
    tpl.add_argument("--config", default="config.yaml")
    tpl.add_argument("--out")
    tpl.set_defaults(func=_template)
    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except (ConfigError, InputError, CalculationError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
