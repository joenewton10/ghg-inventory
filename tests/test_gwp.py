import pytest

from ghg_inventory.gwp import UnknownGasError, decompose, gwp


def test_r410a_ar5_matches_defra_published_value():
    # DEFRA 2025 lists R410A at 1924 kg CO2e/kg (AR5); 50/50 R-32/R-125 gives 1923.5.
    assert gwp("R-410A", "AR5") == pytest.approx(1924, abs=1)


def test_r407c_ar5_matches_defra_published_value():
    assert gwp("R407C", "AR5") == pytest.approx(1624, abs=1)


def test_ar6_values():
    assert gwp("R-32", "AR6") == 771
    assert gwp("R-410A", "AR6") == pytest.approx(0.5 * 771 + 0.5 * 3740)
    assert gwp("CH4", "AR6") == 29.8


def test_blend_decomposes_into_kyoto_gases():
    assert decompose("R410A") == {"HFC-32": 0.5, "HFC-125": 0.5}
    assert decompose("R-32") == {"HFC-32": 1.0}


def test_unknown_gas_raises():
    with pytest.raises(UnknownGasError):
        gwp("R-999", "AR6")
