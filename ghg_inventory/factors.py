"""Factor libraries, per-gas conversion and unit handling."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from ghg_inventory.gwp import GWP_SETS
from ghg_inventory.models import Factor, FactorLibrary, UnitConversion


class FactorError(ValueError):
    pass


class UnitError(ValueError):
    pass


def load_factor_library(path: str | Path) -> dict[str, Factor]:
    """Load one factor library YAML into {"library_id.key": Factor}."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        library = FactorLibrary.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise FactorError(f"factor library {path}: {exc}") from exc
    return {
        f"{library.library_id}.{key}": Factor(factor_id=f"{library.library_id}.{key}", entry=entry, library=library)
        for key, entry in library.factors.items()
    }


def load_factor_libraries(paths: list[str | Path]) -> dict[str, Factor]:
    factors: dict[str, Factor] = {}
    for path in paths:
        for factor_id, factor in load_factor_library(path).items():
            if factor_id in factors:
                raise FactorError(f"factor {factor_id!r} is defined twice")
            factors[factor_id] = factor
    return factors


def get_factor(factors: dict[str, Factor], factor_id: str) -> Factor:
    try:
        return factors[factor_id]
    except KeyError:
        raise FactorError(f"factor {factor_id!r} is not in any loaded factor library") from None


def gas_mass_per_unit(factor: Factor) -> dict[str, float]:
    """kg of each gas per activity unit.

    Libraries publish per-gas values as kg CO2e under their own GWP basis. Dividing by that
    basis recovers the physical mass, so the inventory can apply any GWP set consistently.
    """
    basis = GWP_SETS[factor.library.gwp_basis]
    return {gas: value / basis[gas] for gas, value in factor.entry.co2e_by_gas.items()}


# ---------------------------------------------------------------- units

_UNIT_ALIASES = {
    "kwh": "kWh", "mwh": "MWh", "gj": "GJ",
    "l": "litre", "liter": "litre", "litre": "litre", "litres": "litre", "liters": "litre",
    "m3": "m3", "m³": "m3", "cbm": "m3",
    "kg": "kg", "t": "tonne", "tonne": "tonne", "tonnes": "tonne",
}

_BUILTIN_CONVERSIONS = {
    ("MWh", "kWh"): 1000.0,
    ("GJ", "kWh"): 1000.0 / 3.6,
    ("kg", "tonne"): 0.001,
    ("tonne", "kg"): 1000.0,
}

UNIT_CHOICES = ("kWh", "MWh", "litre", "m3", "kg", "tonne")


def normalise_unit(unit: str) -> str:
    key = unit.strip().lower()
    if key not in _UNIT_ALIASES:
        raise UnitError(f"unknown unit {unit!r}")
    return _UNIT_ALIASES[key]


def convert_quantity(
    quantity: float,
    unit: str,
    target_unit: str,
    source_conversions: dict[str, UnitConversion] | None = None,
) -> float:
    """Convert quantity into the factor's unit, using source-specific conversions first."""
    unit = normalise_unit(unit)
    target = normalise_unit(target_unit)
    if unit == target:
        return quantity
    for raw_unit, conversion in (source_conversions or {}).items():
        if normalise_unit(raw_unit) == unit:
            return quantity * conversion.multiplier
    if (unit, target) in _BUILTIN_CONVERSIONS:
        return quantity * _BUILTIN_CONVERSIONS[(unit, target)]
    raise UnitError(f"no conversion from {unit!r} to {target!r}")
