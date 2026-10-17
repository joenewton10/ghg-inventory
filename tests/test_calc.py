import pytest

from conftest import activity, data, refrigerant
from ghg_inventory.calc import CalculationError, calculate_lines, energy_balance, summarise
from ghg_inventory.models import MarketInstrument, SupplierSpecific


def _ar5(config):
    return config.model_copy(update={"reporting": config.reporting.model_copy(update={"gwp_set": "AR5"})})


def test_electricity_location_and_market(loaded):
    config, factors, _ = loaded
    lines = calculate_lines(config, factors, data(activity("grid_electricity", 1000, "kWh")))
    by_method = {l.method: l for l in lines}
    assert by_method["location"].co2e_kg == pytest.approx(344.0)     # 1000 kWh x 0.344 (UBA 2025)
    assert by_method["market"].co2e_kg == pytest.approx(701.47)      # residual mix, no GOs


def test_scope1_reconciles_to_defra_published_co2e_under_ar5(loaded):
    config, factors, _ = loaded
    lines = calculate_lines(_ar5(config), factors, data(
        activity("heating_gas", 1000, "kWh", row="A!2"),
        activity("vehicle_diesel", 100, "litre", row="A!3"),
        activity("forklift_lpg", 100, "litre", row="A!4"),
    ))
    co2e = {l.source_id: l.co2e_kg for l in lines}
    assert co2e["heating_gas"] == pytest.approx(182.96)
    assert co2e["vehicle_diesel"] == pytest.approx(257.082)
    assert co2e["forklift_lpg"] == pytest.approx(155.713)


def test_ar6_changes_only_non_co2_gases(loaded):
    config, factors, _ = loaded
    line = calculate_lines(config, factors, data(activity("vehicle_diesel", 1, "litre")))[0]
    expected = 2.53763 + 0.00029 / 28 * 29.8 + 0.0329 / 265 * 273
    assert line.co2e_kg == pytest.approx(expected)
    assert line.biogenic_co2_kg == pytest.approx(0.14)


def test_gas_in_m3_uses_bill_conversion(loaded):
    config, factors, _ = loaded
    line = calculate_lines(_ar5(config), factors, data(activity("heating_gas", 100, "m3")))[0]
    assert line.quantity_in_factor_unit == pytest.approx(1066.24)


def test_unit_mismatch_names_the_row(loaded):
    config, factors, _ = loaded
    with pytest.raises(CalculationError, match="Activity!9"):
        calculate_lines(config, factors, data(activity("vehicle_diesel", 5, "kWh", row="Activity!9")))


def test_unknown_source_names_the_row(loaded):
    config, factors, _ = loaded
    with pytest.raises(CalculationError, match="Activity!4.*steam"):
        calculate_lines(config, factors, data(activity("steam", 5, "kWh", row="Activity!4")))


def test_refrigerant_screening(loaded):
    config, factors, _ = loaded
    line = calculate_lines(_ar5(config), factors, data(refrigerants=[refrigerant("R-410A", charge=3, leak=0.05)]))[0]
    assert line.scope == 1 and line.category == "fugitive"
    assert line.gas_kg == pytest.approx({"HFC-32": 0.075, "HFC-125": 0.075})
    assert line.co2e_kg == pytest.approx(3 * 0.05 * 1923.5)


def test_refrigerant_mass_balance(loaded):
    config, factors, _ = loaded
    rec = refrigerant("R-32", leak=None, method="mass_balance", recharged_kg=0.4)
    line = calculate_lines(_ar5(config), factors, data(refrigerants=[rec]))[0]
    assert line.co2e_kg == pytest.approx(0.4 * 677)


def test_market_hierarchy_go_share_and_supplier_factor(loaded):
    config, factors, _ = loaded
    half_go = MarketInstrument(eac_covered_share=0.5, uncovered_basis="residual_mix",
                               residual_mix_factor="aib_2025.residual_mix_de")
    lines = calculate_lines(config, factors, data(activity("grid_electricity", 1000, "kWh")), market_override=half_go)
    assert next(l for l in lines if l.method == "market").co2e_kg == pytest.approx(350.735)

    supplier = MarketInstrument(eac_covered_share=0.0, uncovered_basis="supplier_specific",
                                residual_mix_factor="aib_2025.residual_mix_de",
                                supplier_specific=SupplierSpecific(kg_co2_per_kwh=0.3, renewable_share=0.6, source="label"))
    lines = calculate_lines(config, factors, data(activity("grid_electricity", 1000, "kWh")), market_override=supplier)
    market = next(l for l in lines if l.method == "market")
    assert market.co2e_kg == pytest.approx(300)
    # location-based does not move when the contract changes
    assert next(l for l in lines if l.method == "location").co2e_kg == pytest.approx(344)


def test_summarise_totals(loaded):
    config, factors, _ = loaded
    lines = calculate_lines(_ar5(config), factors, data(
        activity("grid_electricity", 1000, "kWh", row="A!2"),
        activity("heating_gas", 1000, "kWh", row="A!3", dq=3),
    ))
    t = summarise(lines)
    assert t.scope1_kg == pytest.approx(182.96)
    assert t.scope2_location_kg == pytest.approx(344)
    assert t.scope2_market_kg == pytest.approx(701.47)
    assert t.total_location_kg == pytest.approx(526.96)
    assert t.total_market_kg == pytest.approx(884.43)
    assert t.by_source_kg["grid_electricity"] == pytest.approx({"location": 344, "market": 701.47})
    assert t.weighted_data_quality == pytest.approx((1 * 344 + 3 * 182.96) / 526.96)


def test_zero_quantity_gives_zero(loaded):
    config, factors, _ = loaded
    assert summarise(calculate_lines(config, factors, data(activity("heating_gas", 0, "kWh")))).scope1_kg == 0


def test_energy_balance(loaded):
    config, factors, _ = loaded
    rows = {r.source_id: r for r in energy_balance(config, factors, data(
        activity("grid_electricity", 1000, "kWh", row="A!2"),
        activity("vehicle_diesel", 100, "litre", row="A!3"),
    ))}
    assert rows["grid_electricity"].kwh == 1000
    assert rows["grid_electricity"].renewable_kwh == pytest.approx(16)
    assert rows["vehicle_diesel"].kwh == pytest.approx(1053.1)
