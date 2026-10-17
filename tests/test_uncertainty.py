import math

import pytest

from conftest import activity, data
from ghg_inventory.calc import calculate_lines
from ghg_inventory.uncertainty import monte_carlo


def test_single_line_combines_activity_and_factor_uncertainty(loaded):
    config, factors, _ = loaded
    lines = [l for l in calculate_lines(config, factors, data(activity("grid_electricity", 1000, "kWh", dq=1)))]
    result = monte_carlo(lines, draws=50000, seed=1)["scope2_location"]
    # UBA factor ±5%, metered activity ±2%: sqrt(5^2 + 2^2) = 5.39%
    assert result.half_width_pct == pytest.approx(math.hypot(5, 2), abs=0.2)
    assert result.mean_kg == pytest.approx(344, rel=0.01)
    assert result.low_kg < 344 < result.high_kg


def test_same_factor_across_months_is_fully_correlated(loaded):
    config, factors, _ = loaded
    rows = [activity("heating_gas", 1000, "kWh", month=f"2025-{m:02d}", row=f"A!{m}", dq=1) for m in range(1, 13)]
    result = monte_carlo(calculate_lines(config, factors, data(*rows)), draws=50000, seed=2)["scope1"]
    # The factor error (3%) does not average out over 12 months; activity errors (2% each) do.
    expected = math.hypot(3, 2 / math.sqrt(12))
    assert result.half_width_pct == pytest.approx(expected, abs=0.2)


def test_seeded_runs_repeat(loaded):
    config, factors, _ = loaded
    lines = calculate_lines(config, factors, data(activity("vehicle_diesel", 500, "litre", dq=3)))
    assert monte_carlo(lines, 2000, 7) == monte_carlo(lines, 2000, 7)
