"""Load config.yaml and resolve file paths relative to it."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from ghg_inventory.factors import FactorError, get_factor, load_factor_libraries
from ghg_inventory.models import Config, Factor


class ConfigError(Exception):
    pass


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"])
        lines.append(f"  {location or '(top level)'}: {err['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path) -> tuple[Config, dict[str, Factor], Path]:
    """Return (config, factors, base_dir). Every factor the config references must exist."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping of top-level keys")
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} has invalid values:\n{_format_validation_error(exc)}") from exc

    base_dir = path.resolve().parent
    try:
        factors = load_factor_libraries([base_dir / p for p in config.factor_libraries])
        for factor_id in referenced_factor_ids(config):
            get_factor(factors, factor_id)
    except (FactorError, FileNotFoundError) as exc:
        raise ConfigError(str(exc)) from exc
    return config, factors, base_dir


def referenced_factor_ids(config: Config) -> list[str]:
    ids = []
    for source in config.sources:
        if source.scope == 1:
            ids.append(source.factor)
        else:
            ids.append(source.location_factor)
            ids.append(source.market.residual_mix_factor)
    ids.extend(item.factor for item in config.scope3_screening)
    return ids
