"""Load config.yaml, resolve file paths relative to it, and save edited settings back."""

import shutil
from io import StringIO
from datetime import datetime
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
    except (FactorError, FileNotFoundError) as exc:
        raise ConfigError(str(exc)) from exc
    check_references(config, factors)
    return config, factors, base_dir


def check_references(config: Config, factors: dict[str, Factor]) -> None:
    try:
        for factor_id in referenced_factor_ids(config):
            get_factor(factors, factor_id)
    except FactorError as exc:
        raise ConfigError(str(exc)) from exc


def config_from_dict(raw: dict, factors: dict[str, Factor]) -> Config:
    """Validate an edited settings dict (as produced by Config.model_dump) into a Config."""
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"settings have invalid values:\n{_format_validation_error(exc)}") from exc
    check_references(config, factors)
    return config


def _merge(node, value):
    """Copy changed values from a plain dict/list into a ruamel node, keeping comments and key order."""
    if isinstance(value, dict):
        for key, sub in value.items():
            if key in node and isinstance(node[key], (dict, list)) and isinstance(sub, (dict, list)):
                _merge(node[key], sub)
            elif key not in node or node[key] != sub:
                if key in node or sub not in (None, {}, []):
                    node[key] = sub
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            if i < len(node) and isinstance(node[i], (dict, list)) and isinstance(sub, (dict, list)):
                _merge(node[i], sub)
            elif i < len(node):
                node[i] = sub
            else:
                node.append(sub)
        del node[len(value):]


def save_config(path: str | Path, config: Config) -> Path | None:
    """Write config back to its YAML file, keeping comments. Returns the backup path (None if unchanged)."""
    from ruamel.yaml import YAML

    path = Path(path)
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    yaml_rt.width = 4096
    yaml_rt.representer.add_representer(
        type(None), lambda rep, _: rep.represent_scalar("tag:yaml.org,2002:null", "null"))
    original_text = path.read_text(encoding="utf-8")
    document = yaml_rt.load(original_text)
    _merge(document, config.model_dump(mode="json", exclude_defaults=False))

    buffer = StringIO()
    yaml_rt.dump(document, buffer)
    if buffer.getvalue() == original_text:
        return None
    backup = path.with_name(f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak{path.suffix}")
    shutil.copy2(path, backup)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return backup


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
