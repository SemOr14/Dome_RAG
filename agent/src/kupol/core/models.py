"""Типизированные модели данных Штаба.

Все модели — pydantic v2. Любое время хранится в UTC (aware datetime) и при
сериализации превращается в ISO-строку. Имена полей телеметрии условные
(ASSUMPTION): реальные имена приходят из адаптера и маппятся только в
`adapter/real.py` и `config.yaml`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    """Вернуть текущее время в UTC (aware).

    Returns:
        Текущий момент с `tzinfo=UTC`.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Привести время к UTC.

    Наивное время считается UTC: так проще и однозначно, а адаптеры
    обязаны отдавать aware-время.

    Args:
        value: Исходное время.

    Returns:
        Время с `tzinfo=UTC`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def new_id(prefix: str = "") -> str:
    """Сгенерировать короткий уникальный идентификатор.

    Args:
        prefix: Необязательный префикс (например, `cmd`).

    Returns:
        Строка вида `cmd-1a2b3c4d`.
    """
    short = uuid.uuid4().hex[:8]
    return f"{prefix}-{short}" if prefix else short


class KupolModel(BaseModel):
    """Базовая модель: запрет неизвестных полей и нормализация времени в UTC."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*", mode="after")
    @classmethod
    def _utc_everywhere(cls, value: Any) -> Any:
        """Нормализовать любые datetime-поля к UTC."""
        if isinstance(value, datetime):
            return ensure_utc(value)
        return value


class DeviceStatus(StrEnum):
    """Статус производственного устройства."""

    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    UNKNOWN = "unknown"


class AlertLevel(StrEnum):
    """Уровень тревоги."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Числовой ранг для сравнения уровней (info < warning < critical)."""
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class LineState(KupolModel):
    """Состояние линии распределительного щита."""

    on: bool
    power_w: float = Field(ge=0)


class Telemetry(KupolModel):
    """Снимок телеметрии Комплекса.

    Attributes:
        ts: Время снимка (UTC).
        pv_power_w: Генерация солнечных панелей, Вт.
        battery_soc_pct: Заряд АКБ, %.
        battery_voltage_v: Напряжение АКБ, В.
        battery_temp_c: Температура АКБ, °C (ASSUMPTION: поле добавлено для
            сценария скрытого дрейфа датчика температуры АКБ).
        load_total_w: Суммарная нагрузка, Вт.
        lines: Линии щита: имя -> состояние.
        power_mode: Режим энергосистемы (ASSUMPTION: `eco` или `normal`).
        temp_c: Температура в помещении, °C.
        temp_c_2: Второй датчик температуры, °C (ASSUMPTION: пара для сверки; может отсутствовать).
        humidity_pct: Влажность, %.
        co2_ppm: CO2 по датчикам: id датчика -> ppm.
        vent_mode: Режим вытяжки.
        humidifier_mode: Режим увлажнителя.
        cnc_status: Статус станка с ЧПУ.
        printer_status: Статус 3D-принтера.
        solder_status: Статус паяльной станции.
        comms_ok: Признак наличия связи с Комплексом.
        data_age_s: Возраст данных, с (по версии источника).
    """

    ts: datetime
    pv_power_w: float
    battery_soc_pct: float
    battery_voltage_v: float
    battery_temp_c: float | None = None
    load_total_w: float
    lines: dict[str, LineState] = Field(default_factory=dict)
    power_mode: str = "normal"
    temp_c: float
    temp_c_2: float | None = None
    humidity_pct: float
    co2_ppm: dict[str, float] = Field(default_factory=dict)
    vent_mode: str = "off"
    humidifier_mode: str = "off"
    cnc_status: DeviceStatus = DeviceStatus.UNKNOWN
    printer_status: DeviceStatus = DeviceStatus.UNKNOWN
    solder_status: DeviceStatus = DeviceStatus.UNKNOWN
    comms_ok: bool = True
    data_age_s: float = 0.0

    def flat(self) -> dict[str, float]:
        """Плоское представление всех числовых полей.

        Ключи: скалярные поля как есть, `co2_ppm.<sensor>`,
        `lines.<line>.power_w`, `lines.<line>.on` (1.0/0.0). Именно по этим
        именам настраиваются детекторы и хранится история.

        Returns:
            Словарь `имя поля -> значение`.
        """
        out: dict[str, float] = {
            "pv_power_w": self.pv_power_w,
            "battery_soc_pct": self.battery_soc_pct,
            "battery_voltage_v": self.battery_voltage_v,
            "load_total_w": self.load_total_w,
            "temp_c": self.temp_c,
            "humidity_pct": self.humidity_pct,
            "data_age_s": self.data_age_s,
        }
        if self.battery_temp_c is not None:
            out["battery_temp_c"] = self.battery_temp_c
        if self.temp_c_2 is not None:
            out["temp_c_2"] = self.temp_c_2
        for sensor, value in self.co2_ppm.items():
            out[f"co2_ppm.{sensor}"] = value
        for name, line in self.lines.items():
            out[f"lines.{name}.power_w"] = line.power_w
            out[f"lines.{name}.on"] = 1.0 if line.on else 0.0
        return out

    def get(self, field: str) -> float | None:
        """Получить числовое значение по плоскому имени поля.

        Args:
            field: Имя из `flat()`.

        Returns:
            Значение или `None`, если такого поля в снимке нет.
        """
        return self.flat().get(field)

    def lookup(self, path: str) -> Any:
        """Найти значение по пути через точки, включая нечисловые поля.

        Примеры: `vent_mode`, `lines.cnc.on`, `co2_ppm.co2_a`, `cnc_status`.
        Используется предохранителем для проверки эффекта команды.

        Args:
            path: Путь вида `a.b.c`.

        Returns:
            Значение (enum приводится к строке) или `None`, если пути нет.
        """
        current: Any = self
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, BaseModel):
                current = getattr(current, part, None)
            else:
                return None
            if current is None:
                return None
        if isinstance(current, StrEnum):
            return current.value
        return current


class Alert(KupolModel):
    """Тревога детектора или сверки.

    Attributes:
        id: Идентификатор тревоги.
        ts: Время обнаружения (UTC).
        level: Уровень.
        kind: Тип (`threshold`, `trend`, `jump`, `freeze`, `stale`,
            `actuator_no_effect`, `comms_loss`, `crosscheck` и т.д.).
        field: Поле телеметрии, к которому относится тревога (если есть).
        message: Человекочитаемое описание.
        value: Значение, вызвавшее тревогу.
        details: Дополнительные данные (порог, наклон и т.п.).
        detector: Имя детектора, выдавшего тревогу.
        expected: Ожидаемое значение (то, от чего отсчитано отклонение), если применимо.
        confidence: Оценка уверенности детектора от 0 до 1, если применима.
        window: Ссылка на данные окна: `start`, `end` (ISO-время), `n` — число точек.
    """

    id: str = Field(default_factory=lambda: new_id("al"))
    ts: datetime
    level: AlertLevel
    kind: str
    field: str | None = None
    message: str
    value: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    detector: str = ""
    expected: float | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    window: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Ключ дедупликации: одна и та же проблема по тому же полю."""
        return f"{self.kind}:{self.field or '-'}"


EvidenceSupport = Literal["real", "false", "neutral"]


class Evidence(KupolModel):
    """Свидетельство в пользу «реальный» или «ложный» для одного вердикта.

    Attributes:
        source: Независимый источник: датчик (`co2_ppm.co2_a`), `video`, `energy_balance`,
            `operator`, `probe`, `physics` (физический предел), `context` (контекст работ) и т.д.
            Независимость для «правила двух» считается по различным `source`.
        supports: `real` (сигнал реален), `false` (ложный/сбой) или `neutral`.
        weight: Вес свидетельства от 0 до 1.
        detail: Пояснение для человека и журнала.
        rule: Правило сверки или детектор, породившие свидетельство.
        kind_hint: Какой именно вид «ложного» подсказывает свидетельство
            (`false_signal`, `sensor_fault`, `channel_fault`, `drift`); для `real` не используется.
        ts: Когда свидетельство получено (для затухания со временем).
    """

    source: str
    supports: EvidenceSupport
    weight: float = Field(ge=0.0, le=1.0)
    detail: str = ""
    rule: str = ""
    kind_hint: str = ""
    ts: datetime | None = None


VerdictKind = Literal["real", "false_signal", "sensor_fault", "channel_fault", "drift", "unknown"]
VerdictStatus = Literal["provisional", "confirmed", "refuted"]
FALSE_KINDS = ("false_signal", "sensor_fault", "channel_fault", "drift")


class Verdict(KupolModel):
    """Вердикт сверки источников с накоплением свидетельств.

    Вердикт — не бинарный флаг: у него есть уверенность, список свидетельств, статус
    и время перепроверки; его можно пересмотреть при появлении новых данных.

    Attributes:
        id: Идентификатор вердикта.
        ts: Время последнего пересчёта (UTC).
        kind: `real`, `false_signal`, `sensor_fault`, `channel_fault`, `drift` или `unknown`.
        confidence: Уверенность в выбранном виде, от 0 до 1.
        evidence: Свидетельства (за и против).
        status: `provisional` (создан), `confirmed` (подтверждён перепроверкой),
            `refuted` (опровергнут: заменён пересмотренным вердиктом).
        created_at: Время создания вердикта.
        recheck_at: Когда запланирована автоматическая перепроверка.
        explanation: Пояснение, какие источники сопоставлены и почему.
        rule: Главное (самое весомое) правило сверки.
        fields: Поля, к которым относится вердикт.
    """

    id: str = Field(default_factory=lambda: new_id("vd"))
    ts: datetime
    kind: VerdictKind
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    status: VerdictStatus = "provisional"
    created_at: datetime | None = None
    recheck_at: datetime | None = None
    explanation: str
    rule: str = ""
    fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_created(self) -> Verdict:
        """Время создания по умолчанию равно времени вердикта."""
        if self.created_at is None:
            object.__setattr__(self, "created_at", self.ts)
        return self

    @property
    def is_false(self) -> bool:
        """Вердикт говорит, что сигнал ложный или датчик/канал неисправен."""
        return self.kind in FALSE_KINDS

    def sources_for(self, supports: EvidenceSupport) -> set[str]:
        """Различные независимые источники свидетельств заданной стороны."""
        return {e.source for e in self.evidence if e.supports == supports and e.weight > 0}


class ResponseLevel(StrEnum):
    """Уровень реакции по уверенности и критичности (`core/response.py`)."""

    OBSERVE = "observe"  # только наблюдение и запись в журнал
    PREPARE = "prepare"  # подготовить действие, безопасная проба, предупредить Штаб
    ACT_REVERSIBLE = "act_reversible"  # обратимое малозатратное действие с проверкой
    ACT_FULL = "act_full"  # полное действие по протоколу (с подтверждением, если требует конфиг)

    @property
    def rank(self) -> int:
        """Числовой ранг для сравнения уровней."""
        return ("observe", "prepare", "act_reversible", "act_full").index(self.value)


class ProbeRisk(StrEnum):
    """Риск безопасной пробы."""

    NONE = "none"
    LOW = "low"


class Probe(KupolModel):
    """Безопасная активная проверка для различения гипотез.

    Attributes:
        id: Идентификатор пробы из каталога.
        description: Что делаем и зачем.
        cost_wh: Стоимость по энергии, Вт·ч.
        cost_s: Стоимость по времени, с (сколько занимает проба).
        reversible: Обратима ли проба (в бюджет допускаются только обратимые).
        risk: `none` или `low` (более высокого риска у пробы быть не может).
        expected_if_real: Что ожидаем увидеть, если сигнал реален.
        expected_if_false: Что ожидаем увидеть, если сигнал ложный.
        duration_s: Через сколько секунд оценивать результат.
        command: Команда пробы (только через `Guard`) или `None`.
        human_action: Просьба к Оператору (текстовая команда) или `None`.
        applies_to: Шаблоны полей (fnmatch), для которых проба применима.
    """

    id: str
    description: str
    cost_wh: float = Field(default=0.0, ge=0.0)
    cost_s: float = Field(default=0.0, ge=0.0)
    reversible: bool = True
    risk: ProbeRisk = ProbeRisk.NONE
    expected_if_real: str
    expected_if_false: str
    duration_s: float = Field(default=0.0, ge=0.0)
    command: Command | None = None
    human_action: str | None = None
    applies_to: list[str] = Field(default_factory=list)


class Command(KupolModel):
    """Команда Комплексу.

    Attributes:
        id: Идентификатор команды.
        name: Имя команды (из белого списка в `config.yaml`).
        args: Аргументы команды.
        ts: Время создания (UTC).
        reason: Почему команда подаётся (попадает в журнал).
        costly: Дорогое или необратимое действие: требует двух независимых свидетельств.
        evidence: Свидетельства, на которых основана команда (для правила двух).
        safety_event: Событие безопасности (`smoke`, `operator_stop` …): обходит правило двух,
            но не белый список и диапазоны.
    """

    id: str = Field(default_factory=lambda: new_id("cmd"))
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)
    reason: str = ""
    costly: bool = False
    evidence: list[Evidence] = Field(default_factory=list)
    safety_event: str | None = None


class CommandStatus(StrEnum):
    """Статус обработки команды."""

    REJECTED = "rejected"  # отклонена предохранителем (запрет, белый список, диапазон, частота)
    PENDING = "pending"  # ждёт подтверждения человека
    DENIED = "denied"  # человек отклонил
    EXPIRED = "expired"  # подтверждение не пришло вовремя
    ACCEPTED = "accepted"  # адаптер принял команду
    FAILED = "failed"  # адаптер вернул ошибку или таймаут
    VERIFIED = "verified"  # эффект подтверждён состоянием
    NO_EFFECT = "no_effect"  # команда принята, состояние не изменилось
    DOWNGRADED = "downgraded"  # опасна каскадом: превращена в рекомендацию, не отправлена


class SentCommand(KupolModel):
    """Отправленная команда и ожидаемый отклик в нескольких каналах (для проверки причины и следствия).

    Attributes:
        command: Команда.
        sent_at: Когда отправлена адаптеру.
        executor: Исполнитель (ключ ограничения частоты).
        state_path: Путь в телеметрии, где ожидается новое состояние.
        state_expected: Ожидаемое значение состояния.
        state_before: Значение состояния до отправки.
        power_path: Путь мощности линии исполнителя (второй независимый канал).
        power_before: Мощность до отправки.
        outcome: `awaiting`, `verified`, `no_effect` или `late_verified` (эффект пришёл с опозданием).
        delay_s: Фактическая задержка эффекта (для `verified`/`late_verified`).
    """

    command: Command
    sent_at: datetime
    executor: str
    state_path: str | None = None
    state_expected: Any = None
    state_before: Any = None
    power_path: str | None = None
    power_before: float | None = None
    outcome: str = "awaiting"
    delay_s: float | None = None


class CommandResult(KupolModel):
    """Результат обработки команды.

    Attributes:
        command_id: Идентификатор команды.
        status: Статус обработки.
        message: Пояснение.
        ts: Время результата (UTC).
    """

    command_id: str
    status: CommandStatus
    message: str = ""
    ts: datetime = Field(default_factory=utcnow)


class JournalEntry(KupolModel):
    """Запись журнала решений (поля соответствуют п. 7.3 регламента).

    Attributes:
        ts: Время записи (UTC).
        actor: Кто действовал: человек или агент.
        event: Что зафиксировано (событие, признак отклонения).
        cross_check: Чем проверена достоверность (независимые источники).
        hypothesis: Гипотеза о причине.
        impact: Оценка влияния на людей, оборудование, ресурсы, задачу.
        action: Выбранное действие.
        executor: Исполнитель действия.
        result: Результат проверки действия.
        status: Статус записи/решения.
        kind: Тип записи: `decision` или `llm_call`.
        data: Машинные подробности (для `llm_call`: хэш промпта, вход, сырой
            вывод, разобранное решение, `used_fallback`).
    """

    ts: datetime = Field(default_factory=utcnow)
    actor: str
    event: str
    cross_check: str = ""
    hypothesis: str = ""
    impact: str = ""
    action: str = ""
    executor: str = ""
    result: str = ""
    status: str = ""
    kind: str = "decision"
    data: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(KupolModel):
    """Гипотеза Диагноста.

    Attributes:
        cause: Предполагаемая причина.
        probability: Оценка вероятности от 0 до 1.
        safe_check: Безопасная проверка гипотезы.
        id: Идентификатор гипотезы.
        safe_probe: Идентификатор безопасной пробы из каталога (если есть).
        expected_if_real: Что увидим при пробе, если гипотеза верна.
        expected_if_false: Что увидим при пробе, если гипотеза неверна.
        related_alerts: Ключи тревог, к которым относится гипотеза.
        verdict_id: Вердикт, к которому относится гипотеза.
    """

    id: str = Field(default_factory=lambda: new_id("hyp"))
    cause: str
    probability: float = Field(ge=0.0, le=1.0)
    safe_check: str
    safe_probe: str | None = None
    expected_if_real: str = ""
    expected_if_false: str = ""
    related_alerts: list[str] = Field(default_factory=list)
    verdict_id: str | None = None

    @property
    def description(self) -> str:
        """Описание гипотезы (синоним `cause`)."""
        return self.cause


class RecommendationStatus(StrEnum):
    """Статус рекомендации агента."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AUTO_APPLIED = "auto_applied"
    INFO = "info"  # только информирование, решения не требует
    EXPIRED = "expired"  # человек не ответил вовремя


class Recommendation(KupolModel):
    """Рекомендация агента для человека.

    Attributes:
        id: Идентификатор.
        ts: Время создания (UTC).
        agent: Имя агента (`monitor`, `diagnost`, `energy_planner`).
        title: Краткий заголовок.
        rationale: Обоснование.
        priority: Приоритет 1 (срочно) .. 5 (справочно).
        commands: Предлагаемые команды (проходят только через Guard).
        hypotheses: Ранжированные гипотезы (для Диагноста).
        needs_human: Нужно ли вмешательство человека.
        status: Статус рекомендации.
        used_fallback: Получена ли рекомендация по резервному сценарию.
    """

    id: str = Field(default_factory=lambda: new_id("rec"))
    ts: datetime = Field(default_factory=utcnow)
    agent: str
    title: str
    rationale: str = ""
    priority: int = Field(default=3, ge=1, le=5)
    commands: list[Command] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    needs_human: bool = True
    status: RecommendationStatus = RecommendationStatus.PENDING
    used_fallback: bool = False
