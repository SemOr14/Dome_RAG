"""Тесты основы рантайма Dome: конфиг, окружение, лог агента, состояние, каталог."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kupol.dome.agentlog import AgentLog
from kupol.dome.config import DomeConfigError, load_dome_config
from kupol.dome.env import load_settings, parse_keys_file
from kupol.dome.state import DomeState, flatten_numbers, split_path
from tests.dome_helpers import catalog, set_path, snapshot, state_from

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- конфиг


def test_dome_yaml_is_valid() -> None:
    cfg = load_dome_config(ROOT / "dome.yaml")
    assert cfg.runtime.request_rate_per_s <= 5.0
    assert "reset_interlock" in cfg.policy.allowed["dome_automation_01"]


def test_forbidden_actions_are_not_in_policy() -> None:
    cfg = load_dome_config(ROOT / "dome.yaml")
    flat = {a for acts in cfg.policy.allowed.values() for a in acts}
    for bad in ("block_interlock", "block_automation", "manual_start_zone", "lockdown", "set_thresholds", "unlock_door"):
        assert bad not in flat


def test_unknown_key_in_config_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "d.yaml"
    p.write_text("runtime:\n  bogus: 1\n", encoding="utf-8")
    with pytest.raises(DomeConfigError):
        load_dome_config(p)


def test_missing_config_gives_defaults(tmp_path: Path) -> None:
    assert load_dome_config(tmp_path / "none.yaml").runtime.verify_timeout_s > 0


# --------------------------------------------------------------------------- окружение и ключи


def test_keys_file_is_parsed_and_used_as_fallback(tmp_path: Path) -> None:
    keys = tmp_path / "k.txt"
    keys.write_text("team: TEAMKEY123456\n\ndeepseek: sk-abcdef123456\n", encoding="utf-8")
    assert parse_keys_file(keys) == {"team": "TEAMKEY123456", "deepseek": "sk-abcdef123456"}
    st = load_settings(env={"KEYS_FILE": str(keys)}, root=tmp_path)
    assert st.dome_key == "TEAMKEY123456" and st.deepseek_key == "sk-abcdef123456"
    assert st.sources["DOME_KEY"] == "файл ключей"
    assert not st.can_control  # по умолчанию «только чтение»


def test_environment_beats_file_and_control_needs_explicit_flag(tmp_path: Path) -> None:
    keys = tmp_path / "k.txt"
    keys.write_text("team: FROMFILE123456\n", encoding="utf-8")
    env = {"KEYS_FILE": str(keys), "DOME_KEY": "FROMENV123456", "DOME_ALLOW_CONTROL": "1"}
    st = load_settings(env=env, root=tmp_path)
    assert st.dome_key == "FROMENV123456" and st.can_control
    st2 = load_settings(env={**env, "AGENT_DRY_RUN": "1"}, root=tmp_path)
    assert not st2.can_control  # dry-run приоритетнее


# --------------------------------------------------------------------------- лог агента


def test_agentlog_format_refs_and_secret_scrubbing(tmp_path: Path) -> None:
    log = AgentLog(tmp_path, secrets=["SUPERSECRET123"], run_id="20261024-133005")
    log.set_dome_time({"day": 3, "hour": 16.97})
    d = log.write("decision", situation="s", cause="c", confidence=0.9, evidence=[], plan=["p"], source="rules")
    a = log.write("action", ref=d, request={"type": "control", "key": "SUPERSECRET123"}, response={"success": True})
    log.close()
    lines = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]
    assert log.path.name == "agent_20261024-133005.jsonl"
    assert [r["seq"] for r in lines] == [d, a] == [1, 2]
    assert lines[1]["ref"] == d and lines[0]["dome_time"] == "16:58"
    assert lines[0]["ts"].endswith(("+00:00", "Z")) or "+" in lines[0]["ts"] or "-" in lines[0]["ts"][10:]
    assert "SUPERSECRET123" not in log.path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- состояние


def test_split_path_and_get_with_indexes() -> None:
    assert split_path("smart_panel_01.lines[7].power_w") == ["smart_panel_01", "lines", 7, "power_w"]
    st = state_from("quiet")
    assert st.num("climate_sensor_01.co2_ppm") == pytest.approx(725)
    assert st.get("smart_panel_01.lines[0].name", "").startswith("Производ")
    assert st.line(8)["line"] == 8
    assert st.get("no_such.node.field", "dflt") == "dflt"
    assert st.get("dome_automation_01.interlocks.IL_CO2.state") == "ARMED"


def test_none_and_bool_are_not_numbers() -> None:
    st = state_from("quiet")
    set_path(st, "climate_sensor_01.co2_ppm", None)
    assert st.num("climate_sensor_01.co2_ppm") is None
    assert st.num("fume_extraction_01.enabled") is None  # bool не число


def test_flatten_skips_service_lists() -> None:
    paths = dict(flatten_numbers(snapshot("quiet")["data"]["nodes"]["access_control_01"], "access_control_01"))
    assert not any("passage_events" in p for p in paths)
    assert "access_control_01.doors.FABLAB.lock_battery_pct" in paths


def test_history_series_and_window() -> None:
    st = DomeState()
    for i in range(5):
        st.update({"nodes": {"n": {"v": float(i)}}}, "team", wall=100.0 + i * 10)
    assert [v for _, v in st.series("n.v")] == [0, 1, 2, 3, 4]
    assert [v for _, v in st.series("n.v", window_s=15)] == [3, 4]


def test_frozen_node_detection_by_status_and_age() -> None:
    st = state_from("quiet")
    assert st.frozen_nodes() == []
    st.nodes["radio_01"]["status"] = "UNKNOWN"
    st.nodes["wind_turbine_01"]["updated_at"] -= 100
    assert set(st.frozen_nodes()) == {"radio_01", "wind_turbine_01"}


# --------------------------------------------------------------------------- каталог


def test_catalog_resolves_aliases_and_operator_actions() -> None:
    cat = catalog()
    assert cat.resolve("printer_3d_01", "start_print") == "start_job"
    assert cat.resolve("dome_automation_01", "RELEASE_INTERLOCK") == "reset_interlock"
    assert cat.resolve("printer_3d_01", "pause") is None
    assert cat.resolve("printer_3d_01", "reconnect") == "reconnect"
    assert cat.is_operator_only("printer_3d_01", "remove_part")
    assert cat.resolve("no_node", "x") is None
