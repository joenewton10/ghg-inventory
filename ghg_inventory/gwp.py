"""Global warming potentials (100-year) and refrigerant blend compositions.

AR5: IPCC Fifth Assessment Report, WG1 Chapter 8, Table 8.A.1 (values without climate-carbon feedback,
     the set DEFRA uses).
AR6: IPCC Sixth Assessment Report, WG1 Chapter 7, Table 7.15 / 7.SM.7. CH4 uses the fossil value
     because every combustion source in this inventory burns fossil fuel.
Blend compositions are mass fractions from ASHRAE Standard 34.
"""

GWP_SETS: dict[str, dict[str, float]] = {
    "AR5": {
        "CO2": 1.0,
        "CH4": 28.0,
        "N2O": 265.0,
        "HFC-32": 677.0,
        "HFC-125": 3170.0,
        "HFC-134a": 1300.0,
        "HFC-143a": 4800.0,
        "CO2e": 1.0,
    },
    "AR6": {
        "CO2": 1.0,
        "CH4": 29.8,
        "N2O": 273.0,
        "HFC-32": 771.0,
        "HFC-125": 3740.0,
        "HFC-134a": 1530.0,
        "HFC-143a": 5810.0,
        "CO2e": 1.0,
    },
}

GWP_SOURCES = {
    "AR5": "IPCC AR5 (2013), 100-year GWPs without climate-carbon feedback",
    "AR6": "IPCC AR6 (2021), 100-year GWPs, fossil CH4",
}

BLENDS: dict[str, dict[str, float]] = {
    "R-410A": {"HFC-32": 0.50, "HFC-125": 0.50},
    "R-407C": {"HFC-32": 0.23, "HFC-125": 0.25, "HFC-134a": 0.52},
    "R-404A": {"HFC-125": 0.44, "HFC-143a": 0.52, "HFC-134a": 0.04},
}

_ALIASES = {
    "R32": "HFC-32", "R-32": "HFC-32",
    "R125": "HFC-125", "R-125": "HFC-125",
    "R134A": "HFC-134a", "R-134A": "HFC-134a",
    "R143A": "HFC-143a", "R-143A": "HFC-143a",
    "R410A": "R-410A", "R407C": "R-407C", "R404A": "R-404A",
}

REFRIGERANT_CHOICES = ("R-32", "R-410A", "R-407C", "R-404A", "R-134a")


class UnknownGasError(ValueError):
    pass


def canonical_gas(name: str) -> str:
    """Map a refrigerant or gas name to the key used in GWP_SETS or BLENDS."""
    cleaned = name.strip()
    upper = cleaned.upper().replace(" ", "")
    if upper in _ALIASES:
        return _ALIASES[upper]
    for key in (*GWP_SETS["AR5"], *BLENDS):
        if key.upper() == upper:
            return key
    raise UnknownGasError(f"unknown gas or refrigerant {name!r}")


def decompose(gas: str) -> dict[str, float]:
    """Return {component gas: mass fraction}. A pure gas maps to itself."""
    key = canonical_gas(gas)
    return dict(BLENDS[key]) if key in BLENDS else {key: 1.0}


def gwp(gas: str, gwp_set: str) -> float:
    """GWP of a gas or blend (blend GWP is the mass-weighted sum of its components)."""
    if gwp_set not in GWP_SETS:
        raise ValueError(f"unknown GWP set {gwp_set!r}; use one of {sorted(GWP_SETS)}")
    table = GWP_SETS[gwp_set]
    return sum(fraction * table[component] for component, fraction in decompose(gas).items())
