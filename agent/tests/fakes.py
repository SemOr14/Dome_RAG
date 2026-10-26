"""Общие тестовые двойники."""

from __future__ import annotations

from datetime import datetime

from kupol.adapter.base import KupolAdapter
from kupol.core.models import Command, CommandResult, CommandStatus, Telemetry
from tests.conftest import T0, make_telemetry


class FakeAdapter(KupolAdapter):
    """Адаптер-запись: хранит отправленные команды, время задаётся вручную."""

    def __init__(self) -> None:
        self.sent: list[Command] = []
        self.t = T0
        self.raise_exc: Exception | None = None
        self.status = CommandStatus.ACCEPTED

    def get_telemetry(self) -> Telemetry:
        return make_telemetry()

    def get_history(self, field: str, minutes: int) -> list[tuple[datetime, float]]:
        return []

    def send_command(self, cmd: Command) -> CommandResult:
        if self.raise_exc:
            raise self.raise_exc
        self.sent.append(cmd)
        return CommandResult(command_id=cmd.id, status=self.status, message="ok", ts=self.t)

    def is_connected(self) -> bool:
        return True

    def now(self) -> datetime:
        return self.t
