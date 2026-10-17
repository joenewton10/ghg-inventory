"""Monte Carlo uncertainty on inventory totals.

Follows the approach in the GHG Protocol uncertainty guidance: each activity value and each emission
factor gets a 95% confidence range (±%), modelled as a normal distribution truncated at zero.
One draw per factor is shared by every line that uses it (a factor that is 3% high is 3% high all
year), and one draw per activity row is shared by its location- and market-based lines.
"""

from dataclasses import dataclass

import numpy as np

from ghg_inventory.calc import EmissionLine


@dataclass(frozen=True)
class UncertaintyResult:
    central_kg: float
    mean_kg: float
    low_kg: float       # 2.5th percentile
    high_kg: float      # 97.5th percentile

    @property
    def half_width_pct(self) -> float:
        if not self.central_kg:
            return 0.0
        return (self.high_kg - self.low_kg) / 2 / self.central_kg * 100


_GROUPS = {
    "scope1": lambda l: l.scope == 1,
    "scope2_location": lambda l: l.method == "location",
    "scope2_market": lambda l: l.method == "market",
    "total_location": lambda l: l.method in ("direct", "location"),
    "total_market": lambda l: l.method in ("direct", "market"),
}


def _multipliers(keys: list[str], pct: dict[str, float], draws: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    out = {}
    for key in keys:
        sd = pct[key] / 100 / 1.96
        out[key] = np.clip(rng.normal(1.0, sd, draws), 0.0, None) if sd else np.ones(draws)
    return out


def monte_carlo(lines: list[EmissionLine], draws: int = 10000, seed: int | None = 42) -> dict[str, UncertaintyResult]:
    rng = np.random.default_rng(seed)
    factor_pct = {l.factor_id: l.factor_uncertainty_pct for l in lines}
    activity_pct = {l.row_ref: l.activity_uncertainty_pct for l in lines}
    factor_m = _multipliers(sorted(factor_pct), factor_pct, draws, rng)
    activity_m = _multipliers(sorted(activity_pct), activity_pct, draws, rng)

    samples = {l.line_id: l.co2e_kg * factor_m[l.factor_id] * activity_m[l.row_ref] for l in lines}

    results = {}
    for name, include in _GROUPS.items():
        chosen = [l for l in lines if include(l)]
        total = np.sum([samples[l.line_id] for l in chosen], axis=0) if chosen else np.zeros(draws)
        results[name] = UncertaintyResult(
            central_kg=sum(l.co2e_kg for l in chosen),
            mean_kg=float(np.mean(total)),
            low_kg=float(np.percentile(total, 2.5)),
            high_kg=float(np.percentile(total, 97.5)),
        )
    return results
