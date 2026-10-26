"""Тесты абстрактного адаптера: контракт и значения по умолчанию."""

from __future__ import annotations

from datetime import datetime

import pytest

from kupol.adapter.base import AdapterError, AdapterTimeout, KupolAdapter
from kupol.core.models import Command, CommandResult, CommandStatus, Telemetry
from tests.conftest import make_telemetry


class Dummy(KupolAdapter):
    def get_telemetry(self) -> Telemetry:
        return make_telemetry()

    def get_history(self, field: str, minutes: int) -> list[tuple[datetime, float]]:
        return []

    def send_command(self, cmd: Command) -> CommandResult:
        return CommandResult(command_id=cmd.id, status=CommandStatus.ACCEPTED)

    def is_connected(self) -> bool:
        return True


def test_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        KupolAdapter()  # type: ignore[abstract]


def test_defaults() -> None:
    a = Dummy()
    assert a.now().tzinfo is not None
    a.advance(1.0)  # no-op
    a.close()
    assert a.send_command(Command(name="x")).status is CommandStatus.ACCEPTED


def test_timeout_is_adapter_error() -> None:
    assert issubclass(AdapterTimeout, AdapterError)
