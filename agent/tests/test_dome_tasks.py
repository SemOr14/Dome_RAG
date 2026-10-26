"""Разбор чата Оператора на репликах из журнала купола 14.10."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kupol.dome.playbooks.tasks import TasksPlaybook, unanswered_operator_messages
from tests.dome_helpers import make_ctx, set_path, state_from


def op(mid: int, text: str) -> dict[str, Any]:
    return {"id": mid, "sender": {"role": "operator", "name": "Оператор"}, "to": "agent", "text": text}


def free_state():  # type: ignore[no-untyped-def]
    return state_from("quiet", **{"printer_3d_01.bed_clear": True, "printer_3d_01.state": "IDLE"})


def run(st: Any, tmp_path: Path, text: str, mem: dict | None = None, now: float = 1000.0, mid: int = 1) -> tuple[list, dict]:  # type: ignore[type-arg]
    mem = {} if mem is None else mem
    ctx = make_ctx(st, tmp_path, now=now, mem=mem)
    ctx.chat_new = [op(mid, text)]
    return TasksPlaybook().step(ctx), mem


def acts(intents: list) -> list[tuple[str, str, object]]:  # type: ignore[type-arg]
    return [(s.node, s.action, s.value) for i in intents for s in i.steps]


def test_print_task_starts_job(tmp_path: Path) -> None:
    out, _ = run(free_state(), tmp_path, "Следующее задание: sensor_case.gcode — корпус датчика.")
    assert acts(out) == [("printer_3d_01", "start_job", "sensor_case.gcode")]
    assert out[0].say == "Запустил sensor_case.gcode на принтере." and out[0].priority == 1


def test_cnc_tasks_in_operator_wording(tmp_path: Path) -> None:
    for text, name in (
        ("Фрезер свободен? Нужен pcb_power_board.nc (плата питания).", "pcb_power_board.nc"),
        ("Запусти front_panel_engrave.nc (гравировка передней панели), заготовку закрепила.", "front_panel_engrave.nc"),
        ("Следующее задание: pcb_sensor_hub.nc — плата хаба датчиков.", "pcb_sensor_hub.nc"),
    ):
        out, _ = run(free_state(), tmp_path, text)
        assert acts(out) == [("cnc_01", "start_job", name)], text


def test_unknown_file_gets_short_answer_with_options(tmp_path: Path) -> None:
    out, mem = run(free_state(), tmp_path, "Запусти lid_v9.gcode")
    assert not acts(out) and "нет на принтере" in (out[0].say or "") and "sensor_case.gcode" in (out[0].say or "")
    assert not mem["jobs"]


def test_busy_machine_queues_and_starts_after_it_frees(tmp_path: Path) -> None:
    st = free_state()
    set_path(st, "cnc_01.state", "RUNNING")
    set_path(st, "cnc_01.progress_pct", 40.0)
    out, mem = run(st, tmp_path, "Нужен pcb_power_board.nc")
    assert not acts(out) and "занят" in (out[0].say or "") and mem["jobs"][0]["state"] == "waiting_confirm"
    set_path(st, "cnc_01.state", "IDLE")
    ctx = make_ctx(st, tmp_path, now=1100.0, mem=mem)
    assert TasksPlaybook().step(ctx) == []  # ждём подтверждение, что заготовка закреплена
    ctx.chat_new = [op(2, "Заготовку закрепила, запускай")]
    out2 = TasksPlaybook().step(ctx)
    assert acts(out2) == [("cnc_01", "start_job", "pcb_power_board.nc")]


def test_printer_waits_until_bed_is_clear(tmp_path: Path) -> None:
    st = free_state()
    set_path(st, "printer_3d_01.bed_clear", False)
    out, mem = run(st, tmp_path, "Задание: gear_m1_z24.gcode")
    assert not acts(out) and "деталь" in (out[0].say or "")
    set_path(st, "printer_3d_01.bed_clear", True)
    out2 = TasksPlaybook().step(make_ctx(st, tmp_path, now=1050.0, mem=mem))
    assert acts(out2) == [("printer_3d_01", "start_job", "gear_m1_z24.gcode")]


def test_homing_before_start_when_not_homed(tmp_path: Path) -> None:
    st = free_state()
    set_path(st, "cnc_01.homed", False)
    out, _ = run(st, tmp_path, "Нужен pcb_power_board.nc")
    assert acts(out) == [("cnc_01", "home", None)]


def test_taken_note_is_not_a_new_task(tmp_path: Path) -> None:
    out, mem = run(free_state(), tmp_path, "Сняла pcb_sensor_hub.nc, стол фрезера свободен.")
    assert out == [] and not mem["jobs"]


def test_job_completion_is_reported_for_cnc(tmp_path: Path) -> None:
    st = free_state()
    mem = {"jobs": [{"file": "pcb_power_board.nc", "machine": "cnc_01", "state": "started", "seen_running": True, "started_at": 100.0, "msg": 1}]}
    set_path(st, "cnc_01.file_name", "pcb_power_board.nc")
    set_path(st, "cnc_01.state", "IDLE")
    set_path(st, "cnc_01.progress_pct", 100.0)
    out = TasksPlaybook().step(make_ctx(st, tmp_path, now=900.0, mem=mem))
    assert out and "завершена" in (out[0].say or "")


# --------------------------------------------------------------------------- бокс


def test_unlock_refused_while_printing_with_reason(tmp_path: Path) -> None:
    out, _ = run(state_from("printer_done"), tmp_path, "Печать закончилась, заберу деталь — разблокируй бокс.")
    assert not acts(out) and "печать ещё идёт" in (out[0].say or "")


def test_unlock_when_done_and_cool(tmp_path: Path) -> None:
    st = state_from("quiet", **{"printer_3d_01.bed_temp_c": 28.0})
    out, _ = run(st, tmp_path, "Готово на принтере? Иду за деталью.")
    assert acts(out) == [("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False})]
    assert out[0].say == "Бокс разблокирован (замок 0), стол остыл."


def test_relock_after_operator_took_the_part(tmp_path: Path) -> None:
    st = state_from("quiet", **{"thermal_insulation_01.locks[0]": "UNLOCKED"})
    out, _ = run(st, tmp_path, "Деталь забрала, дверь закрыла.")
    assert acts(out) == [("thermal_insulation_01", "set_lock", {"lock": 0, "locked": True})]
    st2 = state_from("quiet", **{"thermal_insulation_01.locks[0]": "UNLOCKED", "thermal_insulation_01.doors[0]": "OPEN"})
    assert acts(run(st2, tmp_path, "Деталь забрала.")[0]) == []  # дверь ещё открыта


# --------------------------------------------------------------------------- вопросы


def test_status_question_gets_numbers_not_a_lecture(tmp_path: Path) -> None:
    out, _ = run(state_from("quiet"), tmp_path, "Привет, я в куполе. Что по системам?")
    say = out[0].say or ""
    assert "АКБ 40 %" in say and "GRID_ONLY" in say and "фрезер свободен" in say and len(say) < 200


def test_non_operator_messages_are_ignored(tmp_path: Path) -> None:
    ctx = make_ctx(free_state(), tmp_path)
    ctx.chat_new = [{"id": 3, "sender": {"role": "guest", "team": 3}, "text": "Запусти sensor_case.gcode"}]
    assert TasksPlaybook().step(ctx) == []


def test_unanswered_messages_survive_restart() -> None:
    recent = [
        {"id": 1, "sender": {"role": "operator"}, "text": "Привет"},
        {"id": 2, "sender": {"role": "team"}, "text": "Здравствуйте"},
        {"id": 3, "sender": {"role": "operator"}, "text": "Следующее задание: gear_m1_z24.gcode"},
    ]
    assert [m["id"] for m in unanswered_operator_messages(recent)] == [3]
