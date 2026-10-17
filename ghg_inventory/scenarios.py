"""Decision-support figures built on the inventory: abatement options, carbon cost exposure,
intensity metrics and an indicative Scope 3 screen. All pure functions."""

from collections import defaultdict
from dataclasses import dataclass

from ghg_inventory.calc import EmissionLine, Totals, market_factor, to_co2e
from ghg_inventory.factors import convert_quantity, gas_mass_per_unit, get_factor
from ghg_inventory.models import AbatementOption, ActivityData, Config, Factor, MarketInstrument

# ---------------------------------------------------------------- annual activity


def annual_quantities(config: Config, factors: dict[str, Factor], data: ActivityData) -> dict[str, float]:
    """Annual quantity per source, in the unit of the source's (location) factor."""
    totals: dict[str, float] = defaultdict(float)
    for record in data.activities:
        source = config.source(record.source_id)
        factor = get_factor(factors, source.factor if source.scope == 1 else source.location_factor)
        totals[source.id] += convert_quantity(record.quantity, record.unit, factor.unit, source.unit_conversions)
    return dict(totals)


def _per_unit_co2e(config: Config, factors: dict[str, Factor], source_id: str,
                   market: MarketInstrument | None = None) -> tuple[float, float]:
    """(location-basis, market-basis) kg CO2e per unit of a source."""
    source = config.source(source_id)
    gwp_set = config.reporting.gwp_set
    if source.scope == 1:
        value = to_co2e(gas_mass_per_unit(get_factor(factors, source.factor)), gwp_set)
        return value, value
    location = to_co2e(gas_mass_per_unit(get_factor(factors, source.location_factor)), gwp_set)
    per_kwh, _, _ = market_factor(market or source.market, factors)
    return location, to_co2e(per_kwh, gwp_set)


# ---------------------------------------------------------------- abatement


@dataclass(frozen=True)
class AbatementResult:
    name: str
    type: str
    deltas: dict[str, float]          # source_id -> change in annual quantity (negative = less)
    saving_location_t: float
    saving_market_t: float
    capex_eur: float
    annual_energy_saving_eur: float
    annual_cost_eur: float            # annualised capex + opex + premiums - energy savings
    eur_per_t: float | None           # on the market-based saving; None if nothing is saved
    payback_years: float | None
    note: str = ""


def capital_recovery_factor(rate: float, years: float) -> float:
    if rate == 0:
        return 1 / years
    growth = (1 + rate) ** years
    return rate * growth / (growth - 1)


def _role(config: Config, role: str, option: AbatementOption) -> str:
    source = config.source_by_role(role)
    if source is None:
        raise ValueError(f"abatement option {option.name!r} needs a source with role {role!r}")
    return source.id


def _option_effect(option: AbatementOption, config: Config, annual: dict[str, float]):
    """Return (deltas, market_override, extra_annual_cost_eur, note) for one option."""
    p = option.param
    t = option.type
    if t == "compressed_air_leaks":
        elec = _role(config, "electricity", option)
        return {elec: -annual[elec] * p("compressed_air_share_of_electricity") * p("leak_reduction_share")}, None, 0.0, ""
    if t == "led_lighting":
        elec = _role(config, "electricity", option)
        return {elec: -annual[elec] * p("lighting_share_of_electricity") * p("saving_share")}, None, 0.0, ""
    if t == "compressor_heat_recovery":
        elec, gas = _role(config, "electricity", option), _role(config, "heating", option)
        heat = annual[elec] * p("compressor_share_of_electricity") * p("recoverable_heat_share") * p("heating_season_utilisation")
        return {gas: -min(annual[gas], heat / p("boiler_efficiency"))}, None, 0.0, ""
    if t == "rooftop_pv":
        elec = _role(config, "electricity", option)
        own_use = p("kwp") * p("yield_kwh_per_kwp") * p("self_consumption_share")
        return {elec: -min(annual[elec], own_use)}, None, 0.0, "Exported surplus is not credited."
    if t == "heat_pump":
        elec, gas = _role(config, "electricity", option), _role(config, "heating", option)
        gas_cut = annual[gas] * p("gas_replaced_share")
        heat = gas_cut * p("boiler_efficiency")
        return {gas: -gas_cut, elec: heat / p("seasonal_cop")}, None, 0.0, "Adds electricity demand."
    if t == "ev_fleet":
        elec, diesel = _role(config, "electricity", option), _role(config, "vehicles", option)
        litres = annual[diesel] * p("diesel_replaced_share")
        km = litres / p("litres_per_100km") * 100
        return {diesel: -litres, elec: km * p("kwh_per_100km") / 100}, None, 0.0, "Assumes depot charging on the site meter."
    if t == "green_tariff":
        elec = _role(config, "electricity", option)
        current = config.source(elec).market
        override = current.model_copy(update={"eac_covered_share": p("eac_covered_share")})
        # The premium is paid only on the kWh actually covered by guarantees of origin.
        premium = annual[elec] * p("eac_covered_share") * p("premium_eur_per_kwh")
        return {}, override, premium, "Changes the market-based figure only. The grid emits the same."
    raise ValueError(f"unhandled abatement type {t!r}")


def evaluate_abatement(config: Config, factors: dict[str, Factor], data: ActivityData) -> list[AbatementResult]:
    annual = annual_quantities(config, factors, data)
    results = []
    for option in config.abatement:
        deltas, override, extra_cost, note = _option_effect(option, config, annual)
        saving_loc = saving_mkt = 0.0
        for source_id, delta in deltas.items():
            loc, mkt = _per_unit_co2e(config, factors, source_id)
            saving_loc -= delta * loc
            saving_mkt -= delta * mkt
        if override is not None:
            elec = _role(config, "electricity", option)
            _, before = _per_unit_co2e(config, factors, elec)
            _, after = _per_unit_co2e(config, factors, elec, market=override)
            saving_mkt += (annual[elec] + deltas.get(elec, 0.0)) * (before - after)

        energy_saving = -sum(delta * config.energy_prices_eur.get(sid, 0.0) for sid, delta in deltas.items())
        capex = option.param_or("capex_eur", 0.0)
        opex = option.param_or("annual_opex_eur", 0.0)
        lifetime = option.param_or("lifetime_years", 1.0)
        annualised = capex * capital_recovery_factor(config.discount_rate, lifetime) if capex else 0.0
        annual_cost = annualised + opex + extra_cost - energy_saving
        net_cash = energy_saving - opex - extra_cost
        saving_mkt_t = saving_mkt / 1000
        results.append(AbatementResult(
            name=option.name, type=option.type, deltas=deltas,
            saving_location_t=saving_loc / 1000, saving_market_t=saving_mkt_t,
            capex_eur=capex, annual_energy_saving_eur=energy_saving, annual_cost_eur=annual_cost,
            eur_per_t=annual_cost / saving_mkt_t if saving_mkt_t > 0 else None,
            payback_years=capex / net_cash if capex and net_cash > 0 else None,
            note=note,
        ))
    return sorted(results, key=lambda r: (r.eur_per_t is None, r.eur_per_t if r.eur_per_t is not None else 0))


# ---------------------------------------------------------------- carbon price exposure


@dataclass(frozen=True)
class CarbonCost:
    scheme: str
    year: int
    eur_per_t: float
    covered_fossil_co2_t: float
    cost_eur: float


def carbon_cost(config: Config, lines: list[EmissionLine]) -> CarbonCost | None:
    """Fossil CO2 from fuels covered by the national carbon price, times that price.

    An approximation: BEHG charges are passed through in fuel prices and use the scheme's own factors.
    """
    price = config.carbon_price
    if price is None:
        return None
    co2_t = sum(l.gas_kg.get("CO2", 0.0) for l in lines
                if l.scope == 1 and l.category in price.covered_categories) / 1000
    return CarbonCost(price.scheme, price.year, price.eur_per_t_co2, co2_t, co2_t * price.eur_per_t_co2)


# ---------------------------------------------------------------- intensity and scope 3


INTENSITY_METRICS = (
    # metric key, label, numerator unit divisor (kg -> t = 1000, kg = 1), denominator scale
    ("revenue_eur", "t CO2e per EUR million revenue", 1000.0, 1e-6),
    ("material_processed_t", "kg CO2e per tonne of material processed", 1.0, 1.0),
    ("fte", "t CO2e per full-time employee", 1000.0, 1.0),
    ("floor_area_m2", "kg CO2e per m2 floor area", 1.0, 1.0),
)


@dataclass(frozen=True)
class Intensity:
    metric: str
    label: str
    denominator: float
    location_based: float
    market_based: float


def intensities(totals: Totals, metrics: dict[str, float]) -> list[Intensity]:
    out = []
    for key, label, numerator_div, scale in INTENSITY_METRICS:
        value = metrics.get(key)
        if not value:
            continue
        denominator = value * scale
        out.append(Intensity(key, label, value,
                             totals.total_location_kg / numerator_div / denominator,
                             totals.total_market_kg / numerator_div / denominator))
    return out


@dataclass(frozen=True)
class Scope3Screen:
    name: str
    quantity: float | None
    unit: str
    factor_id: str
    citation: str
    estimate_t: float | None
    multiple_of_scope12_market: float | None


def scope3_screening(config: Config, factors: dict[str, Factor], totals: Totals,
                     metrics: dict[str, float]) -> list[Scope3Screen]:
    out = []
    for item in config.scope3_screening:
        factor = get_factor(factors, item.factor)
        qty = metrics.get(item.metric)
        estimate = qty * to_co2e(gas_mass_per_unit(factor), config.reporting.gwp_set) / 1000 if qty else None
        multiple = estimate / (totals.total_market_kg / 1000) if estimate and totals.total_market_kg else None
        out.append(Scope3Screen(item.name, qty, factor.unit, factor.factor_id, factor.citation, estimate, multiple))
    return out
