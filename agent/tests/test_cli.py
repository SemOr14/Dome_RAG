"""Тесты CLI и отчётов."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kupol import main as cli
from kupol.main import load_env_file, main
from kupol.report import build_report, evaluate_detection, find_false_alarms, load_run, replay_lines
from tests.conftest import ROOT

CONFIG = str(ROOT / "config.yaml")
BASIC = str(ROOT / "scenarios" / "basic.yaml")


def simulate(tmp_path: Path, *extra: str) -> Path:
    code = main(["simulate", "--config", CONFIG, "--scenario", BASIC, "--seed", "42", "--speed", "0",
                 "--logs-dir", str(tmp_path / "logs"), "--duration", "700", "--auto-approve", *extra])
    assert code == 0
    (run,) = (tmp_path / "logs").iterdir()
    return run


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return simulate(tmp_path_factory.mktemp("cli"))


def test_simulate_creates_run(run_dir: Path) -> None:
    assert "basic" in run_dir.name
    assert (run_dir / "journal.jsonl").stat().st_size > 0


def test_report_command_writes_markdown(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", str(run_dir)]) == 0
    text = (run_dir / "report.md").read_text(encoding="utf-8")
    for heading in ("## Параметры прогона", "## Хронология", "## Тревоги", "## Распознавание воздействий", "## Использование LLM"):
        assert heading in text
    assert "report.md" in capsys.readouterr().out


def test_report_custom_output(run_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "my.md"
    assert main(["report", str(run_dir), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Отчёт о прогоне")


def test_report_detects_false_signal(run_dir: Path) -> None:
    rows = evaluate_detection(load_run(run_dir))
    fs = next(r for r in rows if r["kind"] == "false_signal")
    assert fs["detected"] and fs["delay_s"] <= 5 and fs.get("correct")
    assert find_false_alarms(load_run(run_dir)) == []


def test_replay_prints_timeline(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["replay", str(run_dir), "--every", "120"]) == 0
    out = capsys.readouterr().out
    assert "SoC" in out and "[alert]" in out and "[verdict]" in out and "вердикт" in out
    lines = list(replay_lines(run_dir))
    assert [t for t, _ in lines] == sorted(t for t, _ in lines)


def test_replay_with_speed_sleeps(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    assert main(["replay", str(run_dir), "--speed", "1000", "--every", "600"]) == 0
    assert slept and sum(slept) < 5


def test_report_on_non_run_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report", str(tmp_path)]) == 2
    assert "не каталог прогона" in capsys.readouterr().out


def test_simulate_bad_config_stops_before_game(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("energy: {battery_capacity_wh: -1}\n", encoding="utf-8")
    assert main(["simulate", "--config", str(bad), "--scenario", BASIC, "--logs-dir", str(tmp_path / "l")]) == 2
    out = capsys.readouterr().out
    assert "Ошибка" in out and not (tmp_path / "l").exists()


def test_simulate_bad_scenario(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate", "--config", CONFIG, "--scenario", str(tmp_path / "none.yaml")]) == 2
    assert "Сценарий не найден" in capsys.readouterr().out


def test_llm_fail_flag_makes_agents_use_fallback(tmp_path: Path) -> None:
    run = simulate(tmp_path, "--llm-fail", "timeout")
    calls = [e for e in load_run(run).entries if e.kind == "llm_call"]
    assert calls and all(c.data["used_fallback"] for c in calls)
    assert "резервный сценарий: " in build_report(run)


def test_llm_fail_requires_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    code = main(["simulate", "--config", CONFIG, "--scenario", BASIC, "--llm-mode", "dev", "--llm-fail", "timeout",
                 "--logs-dir", str(tmp_path / "l")])
    assert code == 2 and "mock" in capsys.readouterr().out


def test_competition_without_key_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_env_file", lambda *a, **k: None)
    code = main(["simulate", "--config", CONFIG, "--scenario", BASIC, "--llm-mode", "competition", "--logs-dir", str(tmp_path / "l")])
    assert code == 2 and "DEEPSEEK_API_KEY" in capsys.readouterr().out


def test_run_command_stops_with_instructions(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Пока real.py не реализован, `run` останавливается с инструкцией (без обращения к сети)."""

    def not_implemented(self: object) -> None:
        raise NotImplementedError("Адаптер реального Комплекса не реализован: см. real.py")

    monkeypatch.setattr(cli.RealAdapter, "get_telemetry", not_implemented)
    assert main(["run", "--config", CONFIG, "--logs-dir", str(tmp_path / "l")]) == 2
    out = capsys.readouterr().out
    assert "MORNING_CHECKLIST" in out and not (tmp_path / "l").exists()


def test_run_command_with_unreachable_complex_does_not_hide_missing_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Связи нет, а map_telemetry не реализован: цикл не крутится вхолостую, а останавливается с инструкцией."""
    from kupol.adapter.base import AdapterError

    calls = {"n": 0}

    def flaky(self: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AdapterError("нет сети")  # пробный запрос в cmd_run
        raise NotImplementedError("map_telemetry не реализован")

    monkeypatch.setattr(cli.RealAdapter, "get_telemetry", flaky)
    assert main(["run", "--config", CONFIG, "--logs-dir", str(tmp_path / "l")]) == 2
    out = capsys.readouterr().out
    assert "map_telemetry не реализован" in out and "MORNING_CHECKLIST" in out
    (run_dir,) = (tmp_path / "l").iterdir()
    assert (run_dir / "journal.jsonl").exists()  # журнал сохранён и в этом случае


def test_dashboard_command_builds_streamlit_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: seen.append(cmd) or 0)
    assert main(["dashboard", "--scenario", BASIC, "--speed", "5", "--port", "8600"]) == 0
    cmd = seen[0]
    assert cmd[1:4] == ["-m", "streamlit", "run"] and cmd[4].endswith("dashboard.py")
    assert "8600" in cmd and cmd[cmd.index("--") + 1 :][-2:] == ["--scenario", BASIC]


def test_env_file_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("# комментарий\nFOO_KUPOL=bar\nQUOTED='x y'\nEMPTY=\nBROKEN\nKEEP=new\n", encoding="utf-8")
    monkeypatch.setenv("KEEP", "old")
    monkeypatch.delenv("FOO_KUPOL", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    load_env_file(env)
    assert os.environ["FOO_KUPOL"] == "bar" and os.environ["QUOTED"] == "x y" and os.environ["KEEP"] == "old"
    load_env_file(tmp_path / "missing.env")  # отсутствие файла — не ошибка
    for k in ("FOO_KUPOL", "QUOTED", "EMPTY"):
        os.environ.pop(k, None)
