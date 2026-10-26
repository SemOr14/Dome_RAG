"""Абстрактный интерфейс адаптера Комплекса.

Вся логика Штаба (состояние, детекторы, агенты, предохранитель) работает только
против `KupolAdapter`. Реализации: `SimulatorAdapter` (разработка и тренировки)
и `RealAdapter` (боевой Комплекс, заглушка под утреннюю адаптацию).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from kupol.core.models import Command, CommandResult, Telemetry, utcnow


class AdapterError(Exception):
    """Ошибка работы с Комплексом (протокол, формат ответа, отказ API)."""


class AdapterTimeout(AdapterError):
    """Комплекс не ответил за отведённое время."""


class KupolAdapter(ABC):
    """Единый интерфейс доступа к Комплексу.

    Реализация обязана быть безопасной для вызова из одного потока главного
    цикла; многопоточный доступ не предполагается.
    """

    @abstractmethod
    def get_telemetry(self) -> Telemetry:
        """Получить текущий снимок телеметрии.

        Returns:
            Снимок с временем `ts` (UTC) момента получения.

        Raises:
            AdapterTimeout: Комплекс не ответил вовремя.
            AdapterError: Ответ получен, но не удалось его разобрать, либо API вернул ошибку.
        """

    @abstractmethod
    def get_history(self, field: str, minutes: int) -> list[tuple[datetime, float]]:
        """Получить историю числового поля за последние минуты.

        Args:
            field: Плоское имя поля (см. `Telemetry.flat`), например `co2_ppm.co2_a`.
            minutes: Глубина истории в минутах.

        Returns:
            Список пар `(время UTC, значение)` по возрастанию времени.
            Пустой список, если истории нет.

        Raises:
            AdapterTimeout: Комплекс не ответил вовремя.
            AdapterError: Источник истории недоступен или поле неизвестно.
        """

    @abstractmethod
    def send_command(self, cmd: Command) -> CommandResult:
        """Отправить команду Комплексу.

        Асинхронное принятие: успешный результат означает только «команда
        принята» (`CommandStatus.ACCEPTED`), но не «состояние изменилось».
        Эффект проверяет `Guard` по последующей телеметрии.

        Вызывать напрямую запрещено: любая команда идёт через `Guard.submit`.

        Args:
            cmd: Команда, уже прошедшая проверки предохранителя.

        Returns:
            Результат приёма (`ACCEPTED` или `FAILED`).

        Raises:
            AdapterTimeout: Комплекс не ответил вовремя.
            AdapterError: Команда не доставлена или отвергнута протоколом.
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Проверить, есть ли связь с Комплексом.

        Returns:
            `True`, если канал связи работает. Метод не бросает исключений.
        """

    def now(self) -> datetime:
        """Текущее время с точки зрения адаптера (UTC).

        Для реального Комплекса — системные часы, для симулятора — виртуальное
        время. Главный цикл использует его, когда телеметрию получить не удалось.

        Returns:
            Текущее время в UTC.
        """
        return utcnow()

    def advance(self, dt_s: float) -> None:
        """Продвинуть время на `dt_s` секунд (имеет смысл только для симулятора).

        В реальном адаптере ничего не делает: время идёт само.

        Args:
            dt_s: Шаг, секунд.
        """

    def close(self) -> None:
        """Освободить ресурсы (соединения, потоки). По умолчанию ничего не делает."""
