"""Адаптер реального Комплекса «Купол» — ЗАГОТОВКА под утреннюю адаптацию.

Реальный API, протокол, имена полей и диапазоны команд пока неизвестны (ASSUMPTION),
поэтому методы интерфейса готовы (таймауты, повторы, перевод ошибок в ``AdapterTimeout``
и ``AdapterError``), а три функции-«переводчика» ниже выбрасывают ``NotImplementedError``
с подсказкой. Весь остальной код Штаба менять не нужно.

Что сделать утром (пошагово, подробности — ``docs/MORNING_CHECKLIST.md``):

1. **Протокол.** Выяснить у организаторов: REST, MQTT или WebSocket. Для REST правятся
   ``base_url`` и пути в ``config.yaml`` (раздел ``real``), метод ``_request`` уже умеет
   таймауты и повторы, токен берётся из переменной окружения ``token_env``.
   Для MQTT/WebSocket заменить ``_request`` (подписка + кэш последнего сообщения) —
   сигнатуры публичных методов остаются прежними.
2. **Телеметрия.** Реализовать ``map_telemetry``: превратить ответ API в ``Telemetry``
   (``kupol.core.models``). Это единственное место, где имена полей Комплекса
   сопоставляются с нашими. Время — в UTC, единицы — Вт, %, В, °C, ppm.
3. **История.** Реализовать ``map_history`` (ответ API → список ``(время UTC, значение)``);
   запрос строится по плоскому имени поля (``co2_ppm.<датчик>``, ``lines.<линия>.power_w``).
4. **Команды.** Реализовать ``encode_command`` (наша ``Command`` → тело запроса API) и
   ``decode_command_result`` (ответ API → ``CommandResult``). Имена команд и диапазоны
   аргументов в ``config.yaml`` (раздел ``guard.commands``) привести к реальным: предохранитель
   пропускает только то, что там перечислено.
5. **Проверка.** ``python -m kupol run --config config.yaml`` делает пробный запрос
   телеметрии при старте; пока ``map_telemetry`` не реализован, запуск остановится с
   понятным сообщением.

Правила регламента, которые нельзя нарушать здесь: не добавлять команды, затрагивающие
аппаратные блокировки (п. 9.2), и не подавать команды вне разрешённых диапазонов (п. 12.1) —
это проверяет ``Guard``, но адаптер не должен «помогать» его обходить.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from kupol.adapter.base import AdapterError, AdapterTimeout, KupolAdapter
from kupol.core.config import RealAdapterConfig
from kupol.core.models import Command, CommandResult, Telemetry

_TODO = (
    "Адаптер реального Комплекса не реализован: см. docstring kupol/adapter/real.py и docs/MORNING_CHECKLIST.md"
)


class RealAdapter(KupolAdapter):
    """Адаптер реального Комплекса (REST-шаблон с таймаутами и повторами)."""

    def __init__(
        self,
        config: RealAdapterConfig,
        env: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Создать адаптер.

        Args:
            config: Раздел ``real`` конфигурации (адрес, пути, таймаут, повторы).
            env: Переменные окружения (по умолчанию ``os.environ``); из них берётся токен.
            client: Готовый HTTP-клиент (в тестах — с ``httpx.MockTransport``).
        """
        self._cfg = config
        self._env = os.environ if env is None else env
        self._client = client or httpx.Client(base_url=config.base_url, timeout=config.timeout_s)

    # ------------------------------------------------------------------ точки адаптации (менять утром)

    def map_telemetry(self, raw: Any) -> Telemetry:
        """Перевести ответ API телеметрии в ``Telemetry``.

        ЗДЕСЬ сопоставляются реальные имена полей с нашими. Пример (имена выдуманы)::

            return Telemetry(
                ts=datetime.fromisoformat(raw["time"]),
                pv_power_w=raw["solar"]["power"],
                battery_soc_pct=raw["battery"]["soc"],
                battery_voltage_v=raw["battery"]["voltage"],
                battery_temp_c=raw["battery"].get("temp"),
                load_total_w=raw["load"]["total"],
                lines={n: LineState(on=l["on"], power_w=l["power"]) for n, l in raw["lines"].items()},
                temp_c=raw["climate"]["temp"],
                humidity_pct=raw["climate"]["humidity"],
                co2_ppm={s["id"]: s["ppm"] for s in raw["climate"]["co2"]},
                vent_mode=raw["climate"]["vent"],
                humidifier_mode=raw["climate"]["humidifier"],
                cnc_status=raw["production"]["cnc"],
                printer_status=raw["production"]["printer"],
                solder_status=raw["production"]["solder"],
                comms_ok=True,
                data_age_s=raw.get("age", 0.0),
            )

        Args:
            raw: Разобранный JSON (или другой объект протокола).

        Returns:
            Снимок телеметрии.

        Raises:
            NotImplementedError: Пока не реализовано.
            AdapterError: Ответ не соответствует ожидаемому формату (в реализации).
        """
        raise NotImplementedError(_TODO)

    def map_history(self, field: str, raw: Any) -> list[tuple[datetime, float]]:
        """Перевести ответ API истории в пары ``(время UTC, значение)``.

        Args:
            field: Плоское имя поля, для которого запрошена история.
            raw: Разобранный ответ API.

        Returns:
            Точки по возрастанию времени.

        Raises:
            NotImplementedError: Пока не реализовано.
        """
        raise NotImplementedError(_TODO)

    def encode_command(self, cmd: Command) -> dict[str, Any]:
        """Превратить нашу команду в тело запроса API.

        Имена команд и аргументы должны соответствовать ``guard.commands`` в ``config.yaml``.
        Пример::

            return {"target": "ventilation", "mode": cmd.args["mode"], "request_id": cmd.id}

        Args:
            cmd: Команда, уже прошедшая предохранитель.

        Returns:
            Тело запроса (JSON-совместимое).

        Raises:
            NotImplementedError: Пока не реализовано.
        """
        raise NotImplementedError(_TODO)

    def decode_command_result(self, cmd: Command, raw: Any) -> CommandResult:
        """Перевести ответ API на команду в ``CommandResult`` (``ACCEPTED`` или ``FAILED``).

        Args:
            cmd: Отправленная команда.
            raw: Разобранный ответ API.

        Returns:
            Результат приёма. ``ACCEPTED`` означает только «принята», эффект проверит ``Guard``.

        Raises:
            NotImplementedError: Пока не реализовано.
        """
        raise NotImplementedError(_TODO)

    # ------------------------------------------------------------------ интерфейс KupolAdapter

    def get_telemetry(self) -> Telemetry:
        """Получить текущую телеметрию.

        Returns:
            Снимок телеметрии.

        Raises:
            AdapterTimeout: Комплекс не ответил за ``timeout_s`` с учётом повторов.
            AdapterError: Ошибка протокола или формата.
            NotImplementedError: ``map_telemetry`` не реализован.
        """
        return self.map_telemetry(self._request("GET", self._cfg.telemetry_path))

    def get_history(self, field: str, minutes: int) -> list[tuple[datetime, float]]:
        """Получить историю поля за последние минуты.

        Args:
            field: Плоское имя поля.
            minutes: Глубина, минут.

        Returns:
            Точки по возрастанию времени.

        Raises:
            AdapterTimeout: Нет ответа.
            AdapterError: Ошибка протокола.
            NotImplementedError: ``map_history`` не реализован.
        """
        raw = self._request("GET", self._cfg.history_path, params={"field": field, "minutes": minutes})
        return self.map_history(field, raw)

    def send_command(self, cmd: Command) -> CommandResult:
        """Отправить команду (только через ``Guard``!).

        Повторы для команд НЕ выполняются: повтор управляющей команды после таймаута мог бы
        исполниться дважды.

        Args:
            cmd: Команда.

        Returns:
            Результат приёма.

        Raises:
            AdapterTimeout: Нет ответа (состояние команды неизвестно — проверит ``Guard``).
            AdapterError: Ошибка протокола.
            NotImplementedError: ``encode_command`` / ``decode_command_result`` не реализованы.
        """
        body = self.encode_command(cmd)
        raw = self._request("POST", self._cfg.command_path, json=body, retries=0)
        return self.decode_command_result(cmd, raw)

    def is_connected(self) -> bool:
        """Есть ли связь: быстрый запрос телеметрии без разбора. Не бросает исключений."""
        try:
            self._request("GET", self._cfg.telemetry_path, retries=0)
        except Exception:  # noqa: BLE001 - метод по контракту не бросает
            return False
        return True

    def close(self) -> None:
        """Закрыть HTTP-клиент."""
        self._client.close()

    # ------------------------------------------------------------------ REST-шаблон

    def _headers(self) -> dict[str, str]:
        """Заголовки запроса (токен из окружения, если задан)."""
        token = self._env.get(self._cfg.token_env, "")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        retries: int | None = None,
    ) -> Any:
        """Выполнить запрос с таймаутом и повторами.

        Args:
            method: HTTP-метод.
            path: Путь относительно ``base_url``.
            params: Query-параметры.
            json: Тело запроса.
            retries: Сколько повторов (по умолчанию из конфига).

        Returns:
            Разобранный JSON ответа.

        Raises:
            AdapterTimeout: Все попытки закончились таймаутом.
            AdapterError: Сетевая ошибка, плохой статус или некорректный JSON.
        """
        attempts = 1 + (self._cfg.retries if retries is None else retries)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, params=params, json=json, headers=self._headers())
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException as exc:
                last = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise AdapterError(f"{method} {path}: HTTP {exc.response.status_code}") from exc
                last = exc
            except httpx.HTTPError as exc:
                last = exc
            except ValueError as exc:
                raise AdapterError(f"{method} {path}: ответ не является JSON") from exc
            if attempt + 1 < attempts:
                time.sleep(min(0.2 * (attempt + 1), 1.0))
        if isinstance(last, httpx.TimeoutException):
            raise AdapterTimeout(f"{method} {path}: нет ответа за {self._cfg.timeout_s:g} с") from last
        raise AdapterError(f"{method} {path}: {last.__class__.__name__ if last else 'ошибка'}") from last
