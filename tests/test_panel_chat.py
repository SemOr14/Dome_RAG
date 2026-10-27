"""Лента чата панели: требования Оператора и ответы агента чередуются по времени приёма, а не по часам Купола."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "services" / "data_api"), str(ROOT / "agent" / "src")]
pytest.importorskip("fastapi")
from data_api.store import Store  # noqa: E402

from integration.runtime import Hub  # noqa: E402
from kupol.dome.runner import AGENT_NAME  # noqa: E402


def test_chat_alternates_sides_when_dome_clock_differs(tmp_path):
    hub = Hub(Store(tmp_path / "db.sqlite3"))
    agent = {"role": "team", "team": 1, "name": AGENT_NAME}
    messages = []
    for i, (req, reply) in enumerate((("Статус принтера?", "Печать идёт, 10 %."), ("Отключи линию 3", "Не отключаю: идёт печать.")), 1):
        hub.emit("operator_request", {"text": req})
        hub.emit("agent_message", {"direction": "out", "to": "operator", "text": reply}, source="agent")
        # Купол (эмулятор) ставит время по своим игровым часам — 1970 год
        messages.append({"id": i, "time": 1000000.0 + i, "sender": agent, "to": "operator", "text": reply})
        hub.emit("telemetry", {"type": "telemetry", "data": {"nodes": {"meeting_01": {"recent_messages": list(messages)}}}},
                 source="dome")
    feed = [(m["from"], m["text"]) for m in hub.chat(hub.run_id)["items"]]
    assert feed == [("operator", "Статус принтера?"), ("agent", "Печать идёт, 10 %."),
                    ("operator", "Отключи линию 3"), ("agent", "Не отключаю: идёт печать.")]
