"""Validated data models for configuration, emission factors and activity data."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ghg_inventory.gwp import UnknownGasError, canonical_gas

# Data quality scale used on every activity row, with the 95% uncertainty (± %) the
# Monte Carlo assigns to activity data of that quality.
DATA_QUALITY: dict[int, tuple[str, float]] = {
    1: ("Metered or invoiced, complete year", 2.0),
    2: ("Primary data, small gaps estimated", 5.0),
    3: ("Estimated from partial primary data", 15.0),
    4: ("Proxy, benchmark or engineering estimate", 30.0),
    5: ("Rough estimate", 50.0),
}

Category = Literal["stationary_combustion", "mobile_combustion", "fugitive", "purchased_electricity"]

CATEGORY_LABELS = {
    "stationary_combustion": "Stationary combustion",
    "mobile_combustion": "Mobile combustion",
    "fugitive": "Fugitive (refrigerants)",
    "purchased_electricity": "Purchased electricity",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- emission factors


class FactorEntry(_Frozen):
    description: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    co2e_by_gas: dict[str, float] = Field(min_length=1)
    published_total_co2e: float | None = None
    biogenic_co2_kg_per_unit: float = Field(default=0.0, ge=0)
    energy_kwh_per_unit: float | None = Field(default=None, gt=0)
    renewable_energy_share: float | None = Field(default=None, ge=0, le=1)
    uncertainty_pct: float = Field(ge=0)
    reference: str = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def _total_matches_gases(self):
        if self.published_total_co2e is not None:
            total = sum(self.co2e_by_gas.values())
            if abs(total - self.published_total_co2e) > 1e-4 * max(1.0, self.published_total_co2e):
                raise ValueError(
                    f"per-gas values sum to {total:.6g}, published total is {self.published_total_co2e:.6g}"
                )
        return self


class FactorLibrary(_Frozen):
    library_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    year: int
    version: str = Field(min_length=1)
    url: str = Field(min_length=1)
    gwp_basis: Literal["AR5", "AR6"]
    factors: dict[str, FactorEntry] = Field(min_length=1)


class Factor(_Frozen):
    """A factor entry resolved with its library's citation details."""

    factor_id: str
    entry: FactorEntry
    library: FactorLibrary

    @property
    def unit(self) -> str:
        return self.entry.unit

    @property
    def citation(self) -> str:
        return f"{self.library.publisher}, {self.library.title} ({self.library.year}). {self.entry.reference}"


# ---------------------------------------------------------------- configuration


class Organisation(_Frozen):
    name: str = Field(min_length=1)
    site: str = Field(min_length=1)
    activity: str = Field(min_length=1)


class Reporting(_Frozen):
    year: int
    base_year: int
    recalculation_threshold_pct: float = Field(gt=0)
    consolidation_approach: Literal["operational control", "financial control", "equity share"]
    data_status: Literal["representative", "actual"]
    gwp_set: Literal["AR5", "AR6"]


class UnitConversion(_Frozen):
    multiplier: float = Field(gt=0)
    note: str = ""


class SupplierSpecific(_Frozen):
    kg_co2_per_kwh: float = Field(ge=0)
    renewable_share: float = Field(ge=0, le=1)
    source: str = Field(min_length=1)


class MarketInstrument(_Frozen):
    eac_covered_share: float = Field(ge=0, le=1)
    uncovered_basis: Literal["residual_mix", "supplier_specific"]
    residual_mix_factor: str
    supplier_specific: SupplierSpecific | None = None

    @model_validator(mode="after")
    def _supplier_present(self):
        if self.uncovered_basis == "supplier_specific" and self.supplier_specific is None:
            raise ValueError("uncovered_basis is supplier_specific but supplier_specific is not set")
        return self


class Source(_Frozen):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1)
    role: str | None = None
    scope: Literal[1, 2]
    category: Category
    factor: str | None = None
    location_factor: str | None = None
    market: MarketInstrument | None = None
    unit_conversions: dict[str, UnitConversion] = {}

    @model_validator(mode="after")
    def _factors_for_scope(self):
        if self.scope == 1:
            if not self.factor:
                raise ValueError(f"scope 1 source {self.id!r} needs factor")
            if self.category == "purchased_electricity":
                raise ValueError(f"source {self.id!r}: purchased electricity is scope 2")
        else:
            if not (self.location_factor and self.market):
                raise ValueError(f"scope 2 source {self.id!r} needs location_factor and market")
        return self


class Scope3Item(_Frozen):
    id: str
    name: str
    metric: str
    factor: str


class CarbonPrice(_Frozen):
    scheme: str
    year: int
    eur_per_t_co2: float = Field(ge=0)
    covered_categories: list[Category]


class AbatementOption(BaseModel):
    """Parameters vary by type; scenarios.py validates the ones each type needs."""

    model_config = ConfigDict(extra="allow", frozen=True)
    type: Literal[
        "compressed_air_leaks", "compressor_heat_recovery", "led_lighting",
        "rooftop_pv", "heat_pump", "ev_fleet", "green_tariff",
    ]
    name: str = Field(min_length=1)

    def param(self, key: str) -> float:
        extra = self.model_extra or {}
        if key not in extra:
            raise ValueError(f"abatement option {self.name!r} ({self.type}) is missing {key!r}")
        return float(extra[key])

    def param_or(self, key: str, default: float) -> float:
        return float((self.model_extra or {}).get(key, default))


class MonteCarloSettings(_Frozen):
    draws: int = Field(default=10000, ge=100)
    seed: int | None = 42


class RefrigerantSettings(_Frozen):
    category: Literal["fugitive"] = "fugitive"


class Config(_Frozen):
    organisation: Organisation
    reporting: Reporting
    factor_libraries: list[str] = Field(min_length=1)
    input: str
    sources: list[Source] = Field(min_length=1)
    refrigerants: RefrigerantSettings = RefrigerantSettings()
    scope3_screening: list[Scope3Item] = []
    carbon_price: CarbonPrice | None = None
    energy_prices_eur: dict[str, float] = {}
    discount_rate: float = Field(default=0.06, ge=0)
    abatement: list[AbatementOption] = []
    monte_carlo: MonteCarloSettings = MonteCarloSettings()
    output_dir: str = "output"

    @model_validator(mode="after")
    def _cross_checks(self):
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        unknown = set(self.energy_prices_eur) - set(ids)
        if unknown:
            raise ValueError(f"energy_prices_eur names unknown sources: {sorted(unknown)}")
        if self.reporting.base_year > self.reporting.year:
            raise ValueError("base_year cannot be after the reporting year")
        return self

    def source(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)

    def source_by_role(self, role: str) -> Source | None:
        return next((s for s in self.sources if s.role == role), None)


# ---------------------------------------------------------------- activity data

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ActivityRecord(_Frozen):
    row_ref: str
    source_id: str
    month: str
    quantity: float = Field(ge=0)
    unit: str = Field(min_length=1)
    data_quality: int = Field(ge=1, le=5)
    evidence_ref: str = ""
    notes: str = ""

    @field_validator("month")
    @classmethod
    def _month_format(cls, value: str) -> str:
        if not _MONTH.match(value):
            raise ValueError(f"month must look like 2025-01, got {value!r}")
        return value


class RefrigerantRecord(_Frozen):
    row_ref: str
    equipment: str = Field(min_length=1)
    refrigerant: str = Field(min_length=1)
    charge_kg: float = Field(ge=0)
    method: Literal["screening", "mass_balance"]
    annual_leak_rate: float | None = Field(default=None, ge=0, le=1)
    recharged_kg: float | None = Field(default=None, ge=0)
    data_quality: int = Field(ge=1, le=5)
    evidence_ref: str = ""

    @field_validator("refrigerant")
    @classmethod
    def _known_refrigerant(cls, value: str) -> str:
        try:
            canonical_gas(value)
        except UnknownGasError as exc:
            raise ValueError(str(exc)) from None
        return value

    @model_validator(mode="after")
    def _method_inputs(self):
        if self.method == "screening" and self.annual_leak_rate is None:
            raise ValueError("screening method needs annual_leak_rate")
        if self.method == "mass_balance" and self.recharged_kg is None:
            raise ValueError("mass_balance method needs recharged_kg (refrigerant topped up during the year)")
        return self

    @property
    def released_kg(self) -> float:
        if self.method == "mass_balance":
            return float(self.recharged_kg)
        return self.charge_kg * float(self.annual_leak_rate)


class ActivityData(_Frozen):
    activities: list[ActivityRecord]
    refrigerants: list[RefrigerantRecord] = []
    metrics: dict[str, float] = {}
