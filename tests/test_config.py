from pathlib import Path

import pytest
import yaml

from ghg_inventory.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path, mutate):
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["factor_libraries"] = [str(ROOT / p) for p in raw["factor_libraries"]]
    mutate(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_project_config_loads(loaded):
    config, factors, _ = loaded
    assert config.reporting.gwp_set == "AR6"
    assert {s.id for s in config.sources} >= {"grid_electricity", "heating_gas"}
    assert "aib_2025.residual_mix_de" in factors


def test_missing_factor_reference(tmp_path):
    path = _write(tmp_path, lambda r: r["sources"][0].update(factor="defra_2025.coal"))
    with pytest.raises(ConfigError, match="defra_2025.coal"):
        load_config(path)


def test_scope2_needs_market(tmp_path):
    path = _write(tmp_path, lambda r: r["sources"][3].pop("market"))
    with pytest.raises(ConfigError, match="location_factor and market"):
        load_config(path)


def test_bad_scope(tmp_path):
    path = _write(tmp_path, lambda r: r["sources"][0].update(scope=3))
    with pytest.raises(ConfigError, match="scope"):
        load_config(path)


def test_supplier_basis_needs_supplier_values(tmp_path):
    path = _write(tmp_path, lambda r: r["sources"][3]["market"].update(uncovered_basis="supplier_specific"))
    with pytest.raises(ConfigError, match="supplier_specific"):
        load_config(path)


def test_price_for_unknown_source(tmp_path):
    path = _write(tmp_path, lambda r: r["energy_prices_eur"].update(steam=1.0))
    with pytest.raises(ConfigError, match="steam"):
        load_config(path)


def test_unknown_key_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r.update(scope3_everything=True))
    with pytest.raises(ConfigError, match="scope3_everything"):
        load_config(path)
