"""Тесты загрузки и валидации конфигурации."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kupol.core.config import AppConfig, ConfigError, load_config
from tests.conftest import ROOT


def raw_config() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_repo_config_is_valid(config: AppConfig) -> None:
    assert config.llm.mode == "mock"
    assert "set_line" in config.guard.commands
    assert config.guard.commands["vent_enable"].klass == "auto"
    assert config.guard.commands["set_vent_mode"].klass == "needs_human"


def test_thresholds_for_exact_and_pattern(config: AppConfig) -> None:
    exact = config.thresholds_for("temp_c")
    assert exact is not None and exact.warn_high == 28
    pattern = config.thresholds_for("co2_ppm.co2_a")
    assert pattern is not None and pattern.crit_high == 2000
    assert config.thresholds_for("lines.cnc.on") is None


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="не найден"):
        load_config(tmp_path / "nope.yaml")


def test_broken_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(p)


def test_root_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="словарём"):
        load_config(p)


def test_unknown_key_reported_with_path(tmp_path: Path) -> None:
    data = raw_config()
    data["energy"]["bogus"] = 1
    with pytest.raises(ConfigError, match="energy.bogus"):
        load_config(write(tmp_path, data))


def test_competition_rejects_foreign_model(tmp_path: Path) -> None:
    data = raw_config()
    data["llm"]["mode"] = "competition"
    data["llm"]["model"] = "gpt-4o"
    with pytest.raises(ConfigError, match="competition"):
        load_config(write(tmp_path, data))


def test_competition_accepts_allowed_model(tmp_path: Path) -> None:
    data = raw_config()
    data["llm"]["mode"] = "competition"
    assert load_config(write(tmp_path, data)).llm.mode == "competition"


def test_threshold_order_validated(tmp_path: Path) -> None:
    data = raw_config()
    data["thresholds"]["temp_c"]["crit_high"] = 10
    with pytest.raises(ConfigError, match="crit_high"):
        load_config(write(tmp_path, data))


def test_trend_params_must_be_paired(tmp_path: Path) -> None:
    data = raw_config()
    del data["thresholds"]["temp_c"]["max_slope_per_min"]
    with pytest.raises(ConfigError, match="вместе"):
        load_config(write(tmp_path, data))


def test_verify_arg_must_exist(tmp_path: Path) -> None:
    data = raw_config()
    data["guard"]["commands"]["set_vent_mode"]["verify"]["equals_arg"] = "nope"
    with pytest.raises(ConfigError, match="equals_arg"):
        load_config(write(tmp_path, data))


def test_device_line_must_exist(tmp_path: Path) -> None:
    data = raw_config()
    data["equipment"]["device_lines"]["cnc_status"] = "ghost"
    with pytest.raises(ConfigError, match="ghost"):
        load_config(write(tmp_path, data))


def test_process_dependency_must_exist(tmp_path: Path) -> None:
    data = raw_config()
    data["process"]["operations"][0]["depends_on"] = ["Нет такого"]
    with pytest.raises(ConfigError, match="неизвестного"):
        load_config(write(tmp_path, data))


def test_process_line_must_exist(tmp_path: Path) -> None:
    data = raw_config()
    data["process"]["operations"][0]["device_line"] = "ghost"
    with pytest.raises(ConfigError, match="ghost"):
        load_config(write(tmp_path, data))


def test_enum_arg_requires_values(tmp_path: Path) -> None:
    data = raw_config()
    data["guard"]["commands"]["set_vent_mode"]["args"]["mode"]["values"] = []
    with pytest.raises(ConfigError, match="enum"):
        load_config(write(tmp_path, data))
