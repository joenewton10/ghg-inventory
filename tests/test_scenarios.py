import pytest

from conftest import activity, data
from ghg_inventory.calc import calculate_lines, summarise
from ghg_inventory.scenarios import (
    annual_quantities, capital_recovery_factor, carbon_cost, evaluate_abatement, intensities, scope3_screening,
)

GAS_AR6 = 0.18259 + 0.00028 / 28 * 29.8 + 0.00009 / 265 * 273


@pytest.fixture
def base(loaded):
    config, factors, _ = loaded
    d = data(
        activity("grid_electricity", 100000, "kWh", row="A!2"),
        activity("heating_gas", 50000, "kWh", row="A!3"),
        activity("vehicle_diesel", 1000, "litre", row="A!4"),
        metrics={"material_purchased_t": 100, "fte": 10, "revenue_eur": 2_000_000},
    )
    return config, factors, d


def _by_type(results):
    return {r.type: r for r in results}


def test_annual_quantities(base):
    config, factors, d = base
    assert annual_quantities(config, factors, d) == {"grid_electricity": 100000, "heating_gas": 50000, "vehicle_diesel": 1000}


def test_crf():
    assert capital_recovery_factor(0.06, 3) == pytest.approx(0.374110, abs=1e-6)
    assert capital_recovery_factor(0.0, 4) == 0.25


def test_compressed_air_leaks(base):
    r = _by_type(evaluate_abatement(*base))["compressed_air_leaks"]
    assert r.deltas == {"grid_electricity": pytest.approx(-7500)}
    assert r.saving_location_t == pytest.approx(2.58)
    assert r.saving_market_t == pytest.approx(7500 * 0.70147 / 1000)
    assert r.annual_energy_saving_eur == pytest.approx(1800)
    assert r.annual_cost_eur == pytest.approx(3000 * 0.374110 - 1800, abs=0.01)
    assert r.eur_per_t < 0                      # pays for itself
    assert r.payback_years == pytest.approx(3000 / 1800)


def test_green_tariff_moves_market_based_only(base):
    r = _by_type(evaluate_abatement(*base))["green_tariff"]
    assert r.saving_location_t == 0
    assert r.saving_market_t == pytest.approx(70.147)
    assert r.eur_per_t == pytest.approx(1000 / 70.147)


def test_heat_pump_nets_off_extra_electricity(base):
    r = _by_type(evaluate_abatement(*base))["heat_pump"]
    assert r.deltas["grid_electricity"] == pytest.approx(15000)
    assert r.saving_location_t == pytest.approx((50000 * GAS_AR6 - 15000 * 0.344) / 1000)


def test_results_sorted_cheapest_first(base):
    costs = [r.eur_per_t for r in evaluate_abatement(*base) if r.eur_per_t is not None]
    assert costs == sorted(costs)


def test_carbon_cost_covers_fuel_co2_only(base):
    config, factors, d = base
    cost = carbon_cost(config, calculate_lines(config, factors, d))
    fossil_co2_t = (50000 * 0.18259 + 1000 * 2.53763) / 1000
    assert cost.covered_fossil_co2_t == pytest.approx(fossil_co2_t)
    assert cost.cost_eur == pytest.approx(fossil_co2_t * 55)


def test_intensity_and_scope3(base):
    config, factors, d = base
    totals = summarise(calculate_lines(config, factors, d))
    by_metric = {i.metric: i for i in intensities(totals, d.metrics)}
    assert by_metric["fte"].location_based == pytest.approx(totals.total_location_kg / 1000 / 10)
    assert by_metric["revenue_eur"].market_based == pytest.approx(totals.total_market_kg / 1000 / 2)
    assert "floor_area_m2" not in by_metric
    screen = scope3_screening(config, factors, totals, d.metrics)[0]
    assert screen.estimate_t == pytest.approx(134.50779)


def test_green_tariff_premium_scales_with_coverage(base):
    config, factors, d = base
    half = config.model_copy(update={"abatement": [
        o.model_copy(update={"eac_covered_share": 0.5}) if o.type == "green_tariff" else o for o in config.abatement]})
    r = _by_type(evaluate_abatement(half, factors, d))["green_tariff"]
    assert r.saving_market_t == pytest.approx(70.147 / 2)
    assert r.annual_cost_eur == pytest.approx(500)
    assert r.eur_per_t == pytest.approx(1000 / 70.147)
