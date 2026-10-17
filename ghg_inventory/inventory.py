"""Run the whole calculation once and hold every result the outputs need."""

from dataclasses import dataclass

from ghg_inventory.calc import EmissionLine, EnergyRow, Totals, calculate_lines, energy_balance, summarise
from ghg_inventory.models import ActivityData, Config, Factor
from ghg_inventory.scenarios import (
    AbatementResult, CarbonCost, Intensity, Scope3Screen,
    carbon_cost, evaluate_abatement, intensities, scope3_screening,
)
from ghg_inventory.uncertainty import UncertaintyResult, monte_carlo


@dataclass(frozen=True)
class InventoryResult:
    config: Config
    factors: dict[str, Factor]
    data: ActivityData
    lines: list[EmissionLine]
    totals: Totals
    energy: list[EnergyRow]
    uncertainty: dict[str, UncertaintyResult]
    abatement: list[AbatementResult]
    carbon_cost: CarbonCost | None
    intensities: list[Intensity]
    scope3: list[Scope3Screen]

    def used_factors(self) -> list[Factor]:
        ids = sorted({l.factor_id for l in self.lines if l.factor_id in self.factors}
                     | {s.factor for s in self.config.scope3_screening})
        return [self.factors[i] for i in ids]


def run_inventory(config: Config, factors: dict[str, Factor], data: ActivityData) -> InventoryResult:
    lines = calculate_lines(config, factors, data)
    totals = summarise(lines)
    return InventoryResult(
        config=config,
        factors=factors,
        data=data,
        lines=lines,
        totals=totals,
        energy=energy_balance(config, factors, data),
        uncertainty=monte_carlo(lines, config.monte_carlo.draws, config.monte_carlo.seed),
        abatement=evaluate_abatement(config, factors, data),
        carbon_cost=carbon_cost(config, lines),
        intensities=intensities(totals, data.metrics),
        scope3=scope3_screening(config, factors, totals, data.metrics),
    )


def framework_mapping(result: InventoryResult) -> list[tuple[str, str, str]]:
    """(framework, disclosure, value) rows linking the inventory to the standards customers ask about."""
    t = result.totals
    mwh = sum(r.kwh for r in result.energy) / 1000
    renewable = sum(r.renewable_kwh for r in result.energy) / 1000
    revenue = next((i for i in result.intensities if i.metric == "revenue_eur"), None)
    per_revenue = (f"{revenue.location_based:,.1f} (LB) / {revenue.market_based:,.1f} (MB) t CO2e per EUR million"
                   if revenue else "revenue not provided")
    s1, s2l, s2m = t.scope1_kg / 1000, t.scope2_location_kg / 1000, t.scope2_market_kg / 1000
    return [
        ("GHG Protocol Corporate Standard", "Scope 1, gross", f"{s1:,.1f} t CO2e"),
        ("GHG Protocol Scope 2 Guidance", "Scope 2, location-based and market-based (dual reporting)",
         f"{s2l:,.1f} t / {s2m:,.1f} t CO2e"),
        ("GHG Protocol Corporate Standard", "Biogenic CO2, reported outside the scopes",
         f"{t.biogenic_co2_kg / 1000:,.2f} t CO2"),
        ("VSME (EFRAG) B3", "Total energy consumption, renewable and non-renewable",
         f"{mwh:,.1f} MWh, of which {renewable:,.1f} MWh renewable"),
        ("VSME (EFRAG) B3", "Gross Scope 1 and Scope 2 emissions", f"{s1:,.1f} t; {s2l:,.1f} t (LB), {s2m:,.1f} t (MB)"),
        ("VSME (EFRAG) B3", "GHG intensity per turnover", per_revenue),
        ("ESRS E1-5", "Energy consumption and mix", f"{mwh:,.1f} MWh total, {renewable / mwh:.0%} renewable" if mwh else "n/a"),
        ("ESRS E1-6", "Gross Scope 1; Scope 2 location- and market-based; total",
         f"{s1:,.1f}; {s2l:,.1f} / {s2m:,.1f}; {t.total_location_kg / 1000:,.1f} / {t.total_market_kg / 1000:,.1f} t CO2e"),
        ("ESRS E1-6", "GHG intensity per net revenue", per_revenue),
        ("ISO 14064-1:2018", "Category 1, direct emissions and removals", f"{s1:,.1f} t CO2e"),
        ("ISO 14064-1:2018", "Category 2, indirect emissions from imported energy", f"{s2l:,.1f} t CO2e (location-based)"),
    ]
