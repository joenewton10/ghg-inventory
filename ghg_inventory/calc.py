"""Inventory calculation: activity x factor, per gas, then CO2e. Pure functions, no I/O."""

from collections import defaultdict
from dataclasses import dataclass, field

from ghg_inventory.factors import UnitError, convert_quantity, gas_mass_per_unit, get_factor
from ghg_inventory.gwp import decompose, gwp
from ghg_inventory.models import DATA_QUALITY, ActivityData, Config, Factor, MarketInstrument


class CalculationError(ValueError):
    pass


@dataclass(frozen=True)
class EmissionLine:
    line_id: str
    row_ref: str
    source_id: str
    source_name: str
    scope: int
    category: str
    method: str                 # "direct" (scope 1), "location" or "market" (scope 2)
    month: str
    quantity: float
    unit: str
    quantity_in_factor_unit: float
    factor_unit: str
    factor_id: str
    factor_description: str
    gas_kg: dict[str, float]
    co2e_kg: float
    biogenic_co2_kg: float
    data_quality: int
    evidence_ref: str
    activity_uncertainty_pct: float
    factor_uncertainty_pct: float

    @property
    def co2e_t(self) -> float:
        return self.co2e_kg / 1000.0


@dataclass(frozen=True)
class Totals:
    scope1_kg: float
    scope2_location_kg: float
    scope2_market_kg: float
    scope1_by_category_kg: dict[str, float]
    by_source_kg: dict[str, dict[str, float]]     # source_id -> {"location": kg, "market": kg}
    by_gas_kg: dict[str, float]                   # scope 1 + scope 2 location-based, kg of each gas
    by_month_kg: dict[str, dict[str, float]]      # month -> {source_id: kg} (location-based)
    biogenic_co2_kg: float
    weighted_data_quality: float
    source_names: dict[str, str] = field(default_factory=dict)

    @property
    def total_location_kg(self) -> float:
        return self.scope1_kg + self.scope2_location_kg

    @property
    def total_market_kg(self) -> float:
        return self.scope1_kg + self.scope2_market_kg


def gas_masses(quantity: float, mass_per_unit: dict[str, float]) -> dict[str, float]:
    return {gas: quantity * per_unit for gas, per_unit in mass_per_unit.items()}


def to_co2e(gas_kg: dict[str, float], gwp_set: str) -> float:
    return sum(kg * gwp(gas, gwp_set) for gas, kg in gas_kg.items())


def market_factor(market: MarketInstrument, factors: dict[str, Factor]) -> tuple[dict[str, float], str, float]:
    """Effective market-based kg CO2 per kWh, its description, and its uncertainty.

    kWh covered by cancelled guarantees of origin count at zero. The rest takes the supplier's
    disclosed factor (Stromkennzeichnung) when available, otherwise the residual mix.
    """
    uncovered = 1.0 - market.eac_covered_share
    if market.uncovered_basis == "supplier_specific":
        per_kwh = market.supplier_specific.kg_co2_per_kwh
        basis = f"supplier-specific: {market.supplier_specific.source}"
        uncertainty = 5.0
    else:
        residual = get_factor(factors, market.residual_mix_factor)
        per_kwh = gas_mass_per_unit(residual)["CO2"]
        basis = f"residual mix ({residual.factor_id})"
        uncertainty = residual.entry.uncertainty_pct
    description = f"{market.eac_covered_share:.0%} GO-backed at 0 g; {uncovered:.0%} at {basis}"
    return {"CO2": per_kwh * uncovered}, description, uncertainty


def _quantity_in(record, source, target_unit: str) -> float:
    try:
        return convert_quantity(record.quantity, record.unit, target_unit, source.unit_conversions)
    except UnitError as exc:
        hint = ""
        if record.unit.strip().lower() in ("m3", "m³"):
            hint = f"; add Zustandszahl x Brennwert under sources.{source.id}.unit_conversions.m3"
        raise CalculationError(f"{record.row_ref}: {exc} for source {source.id!r}{hint}") from exc


def calculate_lines(
    config: Config,
    factors: dict[str, Factor],
    data: ActivityData,
    market_override: MarketInstrument | None = None,
) -> list[EmissionLine]:
    """One line per activity row (two for scope 2: location and market), plus one per refrigerant row."""
    gwp_set = config.reporting.gwp_set
    lines: list[EmissionLine] = []

    for record in data.activities:
        try:
            source = config.source(record.source_id)
        except KeyError:
            raise CalculationError(f"{record.row_ref}: unknown source {record.source_id!r}") from None
        activity_u = DATA_QUALITY[record.data_quality][1]
        common = dict(
            row_ref=record.row_ref, source_id=source.id, source_name=source.name, scope=source.scope,
            category=source.category, month=record.month, quantity=record.quantity, unit=record.unit,
            data_quality=record.data_quality, evidence_ref=record.evidence_ref,
            activity_uncertainty_pct=activity_u,
        )

        if source.scope == 1:
            factor = get_factor(factors, source.factor)
            qty = _quantity_in(record, source, factor.unit)
            gas_kg = gas_masses(qty, gas_mass_per_unit(factor))
            lines.append(EmissionLine(
                line_id=f"{record.row_ref}|direct", method="direct",
                quantity_in_factor_unit=qty, factor_unit=factor.unit, factor_id=factor.factor_id,
                factor_description=factor.entry.description, gas_kg=gas_kg,
                co2e_kg=to_co2e(gas_kg, gwp_set),
                biogenic_co2_kg=qty * factor.entry.biogenic_co2_kg_per_unit,
                factor_uncertainty_pct=factor.entry.uncertainty_pct, **common,
            ))
            continue

        location = get_factor(factors, source.location_factor)
        qty = _quantity_in(record, source, location.unit)
        gas_kg = gas_masses(qty, gas_mass_per_unit(location))
        lines.append(EmissionLine(
            line_id=f"{record.row_ref}|location", method="location",
            quantity_in_factor_unit=qty, factor_unit=location.unit, factor_id=location.factor_id,
            factor_description=location.entry.description, gas_kg=gas_kg,
            co2e_kg=to_co2e(gas_kg, gwp_set), biogenic_co2_kg=0.0,
            factor_uncertainty_pct=location.entry.uncertainty_pct, **common,
        ))

        market = market_override or source.market
        per_kwh, description, market_u = market_factor(market, factors)
        gas_kg = gas_masses(qty, per_kwh)
        lines.append(EmissionLine(
            line_id=f"{record.row_ref}|market", method="market",
            quantity_in_factor_unit=qty, factor_unit="kWh",
            factor_id=(market.residual_mix_factor if market.uncovered_basis == "residual_mix" else "supplier_specific"),
            factor_description=description, gas_kg=gas_kg,
            co2e_kg=to_co2e(gas_kg, gwp_set), biogenic_co2_kg=0.0,
            factor_uncertainty_pct=market_u, **common,
        ))

    for record in data.refrigerants:
        released = record.released_kg
        gas_kg = {gas: released * fraction for gas, fraction in decompose(record.refrigerant).items()}
        how = (f"charge {record.charge_kg:g} kg x leak rate {record.annual_leak_rate:.0%}"
               if record.method == "screening" else f"mass balance, {record.recharged_kg:g} kg topped up")
        lines.append(EmissionLine(
            line_id=f"{record.row_ref}|direct", row_ref=record.row_ref,
            source_id=f"refrigerant_{record.equipment.lower().replace(' ', '_')}",
            source_name=f"{record.equipment} ({record.refrigerant})", scope=1,
            category=config.refrigerants.category, method="direct", month=str(config.reporting.year),
            quantity=released, unit="kg", quantity_in_factor_unit=released, factor_unit="kg",
            factor_id=f"gwp.{gwp_set}.{record.refrigerant}",
            factor_description=f"{record.refrigerant} GWP {gwp(record.refrigerant, gwp_set):g} ({how})",
            gas_kg=gas_kg, co2e_kg=to_co2e(gas_kg, gwp_set), biogenic_co2_kg=0.0,
            data_quality=record.data_quality, evidence_ref=record.evidence_ref,
            activity_uncertainty_pct=DATA_QUALITY[record.data_quality][1], factor_uncertainty_pct=0.0,
        ))

    return lines


def summarise(lines: list[EmissionLine]) -> Totals:
    scope1 = sum(line.co2e_kg for line in lines if line.scope == 1)
    s2_location = sum(line.co2e_kg for line in lines if line.method == "location")
    s2_market = sum(line.co2e_kg for line in lines if line.method == "market")

    by_category: dict[str, float] = defaultdict(float)
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: {"location": 0.0, "market": 0.0})
    by_gas: dict[str, float] = defaultdict(float)
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names: dict[str, str] = {}
    weighted_dq = 0.0

    for line in lines:
        names[line.source_id] = line.source_name
        if line.scope == 1:
            by_category[line.category] += line.co2e_kg
            by_source[line.source_id]["location"] += line.co2e_kg
            by_source[line.source_id]["market"] += line.co2e_kg
        else:
            by_source[line.source_id][line.method] += line.co2e_kg
        if line.method in ("direct", "location"):
            for gas, kg in line.gas_kg.items():
                by_gas[gas] += kg
            by_month[line.month][line.source_id] += line.co2e_kg
            weighted_dq += line.data_quality * line.co2e_kg

    total = scope1 + s2_location
    return Totals(
        scope1_kg=scope1,
        scope2_location_kg=s2_location,
        scope2_market_kg=s2_market,
        scope1_by_category_kg=dict(by_category),
        by_source_kg={k: dict(v) for k, v in by_source.items()},
        by_gas_kg=dict(by_gas),
        by_month_kg={k: dict(v) for k, v in sorted(by_month.items())},
        biogenic_co2_kg=sum(line.biogenic_co2_kg for line in lines),
        weighted_data_quality=weighted_dq / total if total else 0.0,
        source_names=names,
    )


@dataclass(frozen=True)
class EnergyRow:
    source_id: str
    source_name: str
    kwh: float
    renewable_kwh: float


def energy_balance(config: Config, factors: dict[str, Factor], data: ActivityData) -> list[EnergyRow]:
    """Final energy use by source in kWh (gross CV for fuels), with the renewable part.

    Electricity's renewable share follows the contract (GO coverage, then supplier or residual mix),
    which is how VSME B3 and ESRS E1-5 expect it to be split.
    """
    kwh: dict[str, float] = defaultdict(float)
    renewable: dict[str, float] = defaultdict(float)
    for record in data.activities:
        source = config.source(record.source_id)
        if source.scope == 1:
            factor = get_factor(factors, source.factor)
            energy = _quantity_in(record, source, factor.unit) * (factor.entry.energy_kwh_per_unit or 0.0)
            share = factor.entry.renewable_energy_share or 0.0
        else:
            location = get_factor(factors, source.location_factor)
            energy = _quantity_in(record, source, location.unit)
            m = source.market
            if m.uncovered_basis == "supplier_specific":
                uncovered_share = m.supplier_specific.renewable_share
            else:
                uncovered_share = get_factor(factors, m.residual_mix_factor).entry.renewable_energy_share or 0.0
            share = m.eac_covered_share + (1 - m.eac_covered_share) * uncovered_share
        kwh[source.id] += energy
        renewable[source.id] += energy * share
    return [EnergyRow(s.id, s.name, kwh[s.id], renewable[s.id]) for s in config.sources if s.id in kwh]
