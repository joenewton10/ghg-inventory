import pytest

from ghg_inventory.factors import FactorError, UnitError, convert_quantity, gas_mass_per_unit, load_factor_library
from ghg_inventory.models import UnitConversion


def test_all_libraries_load_with_citations(loaded):
    _, factors, _ = loaded
    for factor in factors.values():
        assert factor.entry.reference
        assert factor.library.url.startswith("https://")
        assert factor.library.year == 2025


def test_gas_mass_recovers_physical_kg_from_ar5_co2e(loaded):
    _, factors, _ = loaded
    masses = gas_mass_per_unit(factors["defra_2025.natural_gas_gcv"])
    assert masses["CO2"] == pytest.approx(0.18259)
    assert masses["CH4"] == pytest.approx(0.00028 / 28)
    assert masses["N2O"] == pytest.approx(0.00009 / 265)


def test_per_gas_values_must_add_up(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "library_id: x\ntitle: t\npublisher: p\nyear: 2025\nversion: '1'\nurl: https://x\ngwp_basis: AR5\n"
        "factors:\n  f:\n    description: d\n    unit: kWh\n    co2e_by_gas: {CO2: 0.1, CH4: 0.1}\n"
        "    published_total_co2e: 0.3\n    uncertainty_pct: 1\n    reference: r\n"
    )
    with pytest.raises(FactorError, match="sum to"):
        load_factor_library(bad)


def test_missing_reference_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "library_id: x\ntitle: t\npublisher: p\nyear: 2025\nversion: '1'\nurl: https://x\ngwp_basis: AR5\n"
        "factors:\n  f:\n    description: d\n    unit: kWh\n    co2e_by_gas: {CO2: 0.1}\n    uncertainty_pct: 1\n"
    )
    with pytest.raises(FactorError, match="reference"):
        load_factor_library(bad)


def test_unit_conversions():
    assert convert_quantity(2, "MWh", "kWh") == 2000
    assert convert_quantity(10, "Litres", "litre") == 10
    gas = {"m3": UnitConversion(multiplier=10.66)}
    assert convert_quantity(100, "m³", "kWh", gas) == pytest.approx(1066)
    with pytest.raises(UnitError):
        convert_quantity(100, "m3", "kWh")
    with pytest.raises(UnitError):
        convert_quantity(1, "kWh", "litre")
