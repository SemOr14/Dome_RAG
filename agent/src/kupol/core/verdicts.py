"""Накопление свидетельств и жизненный цикл вердиктов.

Вердикт — не бинарный флаг. Каждое правило сверки (`core/crosscheck.py`) и каждый детектор
(`core/detectors.py`) добавляют `Evidence` (свидетельство за «реальный» или за «ложный» с весом из
конфига) к *утверждению* об одном поле (`subject`). Книга вердиктов (`VerdictBook`) сводит свидетельства
в `Verdict` с уверенностью, видом и статусом.

**Агрегация** — логистическая сумма весов::

    z = bias + scale · Σ (± w_i · 0.5^(возраст_i / half_life)),   p_real = 1 / (1 + e^(−z))

Знак «+» у свидетельств за «реальный», «−» у свидетельств за «ложный». Старые свидетельства теряют вес
(иначе вердикт «ложный» нельзя пересмотреть). Вид вердикта: `p_real ≥ real_above` → `real`;
`p_real ≤ false_below` → вид «ложного» по подсказкам свидетельств (`false_signal`, `sensor_fault`,
`channel_fault`, `drift`); между порогами → `unknown`. Параметры — в `config.yaml` (раздел `verdicts`).

**Жизненный цикл:**

1. вердикт создаётся как `provisional` с `recheck_at = created_at + recheck_after_s`;
2. в `recheck_at` пересчитывается: если вид не изменился и определён — статус `confirmed`;
3. вердикт «ложный» **никогда не закрывается навсегда**: он остаётся под наблюдением ещё
   `watch_after_false_s` после последнего свидетельства; если появляются свидетельства за «реальный»
   (случай `false_then_real`), старый вердикт получает статус `refuted`, создаётся новый, а Штаб получает
   тревогу «пересмотр вердикта»;
4. все смены статуса и их причины возвращаются как события для журнала.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kupol.core.config import AppConfig
from kupol.core.models import (
    FALSE_KINDS,
    Alert,
    AlertLevel,
    Evidence,
    Telemetry,
    Verdict,
    new_id,
)

# Тревоги, которые «открывают» утверждение о поле (даже без свидетельств вердикт виден как unknown).
CLAIM_ALERTS = frozenset(
    {"threshold", "jump", "trend", "time_to_threshold", "rate_limit", "pair_diff", "cusum", "noise_loss", "freeze", "time_monotonic"}
)

# Правила, чей вердикт `real` сам по себе является признаком проблемы: книга выпускает тревогу.
DEFAULT_ALERTING_RULES = frozenset({"plan_vs_fact", "thermal_balance", "cause_effect", "energy_balance"})

KIND_RU = {
    "real": "реальный",
    "false_signal": "ложный сигнал",
    "sensor_fault": "неисправность датчика",
    "channel_fault": "неисправность канала",
    "drift": "дрейф датчика",
    "unknown": "не определён",
}


@dataclass
class SubjectEvidence:
    """Свидетельство, привязанное к утверждению о поле.

    Attributes:
        subject: Поле (или метка пары `a|b`, `energy_balance`, `ts`, `actuator:<исполнитель>`).
        fields: Связанные поля для вердикта.
        evidence: Само свидетельство.
        alerting: Выпускать ли тревогу, пока вердикт `real` (тихий брак, скрытый нагрев…).
    """

    subject: str
    evidence: Evidence
    fields: list[str] = field(default_factory=list)
    alerting: bool = False


@dataclass
class _Track:
    """Внутреннее состояние одного утверждения."""

    subject: str
    verdict: Verdict
    evidence: dict[tuple[str, str, str], Evidence] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    last_false_ts: datetime | None = None
    last_claim_ts: datetime | None = None
    checked_side: str = "unknown"  # сторона на момент последней перепроверки: real / false / unknown
    last_kind: str = "unknown"  # вид последнего определённого вердикта
    last_determinate_ts: datetime | None = None  # когда вердикт был определённым в последний раз
    last_side: str = "unknown"  # последняя определённая сторона (для пересмотра через промежуточное «не определён»)
    alerting: bool = False
    p_real: float = 0.5  # последняя вероятность «реальный» (для графика; вид и уверенность считаются из неё же)


@dataclass
class BookEvent:
    """Событие жизненного цикла вердикта (для журнала)."""

    event: str  # created | reclassified | confirmed | revised | retired | closed
    verdict: Verdict
    previous: Verdict | None = None
    reason: str = ""


@dataclass
class BookUpdate:
    """Результат одного обновления книги."""

    verdicts: list[Verdict]
    alerts: list[Alert]
    events: list[BookEvent]


def sigmoid(z: float) -> float:
    """Логистическая функция без переполнения."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def side_of(kind: str) -> str:
    """Сторона вердикта: `real`, `false` (любой вид «ложного») или `unknown`."""
    return "real" if kind == "real" else ("false" if kind in FALSE_KINDS else "unknown")


class VerdictBook:
    """Книга вердиктов: накопление свидетельств, перепроверка, пересмотр."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация Штаба (раздел `verdicts`).
        """
        self._cfg = config
        self._v = config.verdicts
        self._tracks: dict[str, _Track] = {}
        self.archive: list[Verdict] = []
        self.revisions = 0

    # ------------------------------------------------------------------ чтение

    def active(self) -> list[Verdict]:
        """Действующие вердикты (сначала более определённые и уверенные)."""
        order = {"real": 0, "unknown": 2}
        return sorted(
            (t.verdict for t in self._tracks.values()),
            key=lambda v: (order.get(v.kind, 1), -v.confidence),
        )

    def probabilities(self) -> dict[str, float]:
        """Вероятность «реальный» по каждому действующему утверждению (предмет → p)."""
        return {subject: t.p_real for subject, t in self._tracks.items()}

    def get(self, subject: str) -> Verdict | None:
        """Действующий вердикт по утверждению о поле."""
        t = self._tracks.get(subject)
        return t.verdict if t else None

    def by_id(self, verdict_id: str) -> Verdict | None:
        """Действующий вердикт по идентификатору."""
        return next((t.verdict for t in self._tracks.values() if t.verdict.id == verdict_id), None)

    # ------------------------------------------------------------------ ввод свидетельств извне

    def add_evidence(self, subject: str, evidence: Evidence, now: datetime, fields: list[str] | None = None) -> None:
        """Добавить внешнее свидетельство (результат пробы, слова Оператора, видео).

        Args:
            subject: Утверждение, к которому относится свидетельство.
            evidence: Свидетельство.
            now: Текущее время.
            fields: Связанные поля (по умолчанию само утверждение).
        """
        track = self._tracks.get(subject) or self._new_track(subject, now, fields or [subject])
        self._merge(track, evidence.model_copy(update={"ts": evidence.ts or now}), now)

    def retract(self, subject: str, rule: str | None = None, supports: str | None = None) -> int:
        """Отозвать свидетельства (например, «отказ исполнителя» после запоздалого срабатывания).

        Args:
            subject: Утверждение.
            rule: Правило, свидетельства которого отзываются (`None` — любые).
            supports: Сторона (`real`/`false`), `None` — любая.

        Returns:
            Сколько свидетельств отозвано.
        """
        track = self._tracks.get(subject)
        if track is None:
            return 0
        keys = [k for k, e in track.evidence.items() if (rule is None or e.rule == rule) and (supports is None or e.supports == supports)]
        for k in keys:
            del track.evidence[k]
        return len(keys)

    def close_manual(self, verdict_id: str, who: str, now: datetime) -> BookEvent | None:
        """Закрыть вердикт вручную (решение человека). Новые свидетельства откроют вердикт заново.

        Args:
            verdict_id: Идентификатор вердикта.
            who: Кто закрыл.
            now: Текущее время.

        Returns:
            Событие закрытия или `None`, если такого вердикта нет.
        """
        for subject, t in list(self._tracks.items()):
            if t.verdict.id == verdict_id:
                closed = t.verdict.model_copy(update={"status": "refuted", "ts": now})
                self.archive.append(closed)
                del self._tracks[subject]
                return BookEvent("closed", closed, reason=f"закрыт вручную: {who}")
        return None

    # ------------------------------------------------------------------ обновление

    def update(
        self,
        now: datetime,
        latest: Telemetry | None,
        rule_verdicts: list[Verdict],
        alerts: list[Alert],
        extra: list[SubjectEvidence] | None = None,
    ) -> BookUpdate:
        """Принять свидетельства такта и пересчитать все вердикты.

        Args:
            now: Текущее время.
            latest: Последний снимок (для разбора правил сверки).
            rule_verdicts: Выходы правил сверки (`CrossChecker.run`).
            alerts: Тревоги такта (детекторы открывают утверждения и дают свидетельства).
            extra: Дополнительные свидетельства (контекст работ, пробы и т.п.).

        Returns:
            Действующие вердикты, выпущенные тревоги и события жизненного цикла.
        """
        events: list[BookEvent] = []
        items: list[SubjectEvidence] = []
        for v in rule_verdicts:
            items += self.evidence_from_rule(v, latest)
        for a in alerts:
            items += self.evidence_from_alert(a)
        items += extra or []
        for it in items:
            track = self._tracks.get(it.subject)
            if track is None:
                track = self._new_track(it.subject, now, it.fields or [it.subject])
            track.alerting = track.alerting or it.alerting
            if it.evidence.weight > 0 or it.evidence.supports != "neutral":
                self._merge(track, it.evidence.model_copy(update={"ts": now}), now)
            else:
                track.last_claim_ts = now
        for a in alerts:
            if a.kind in CLAIM_ALERTS and a.field:
                if a.field not in self._tracks:
                    self._new_track(a.field, now, [a.field])
                self._tracks[a.field].last_claim_ts = now
        out_alerts: list[Alert] = []
        for subject, track in list(self._tracks.items()):
            retire, revision = self._recompute(track, now, events)
            if retire:
                self.archive.append(track.verdict)
                events.append(BookEvent("retired", track.verdict, reason="свидетельства исчерпаны или наблюдение закончено"))
                del self._tracks[subject]
                continue
            if revision is not None:
                out_alerts.append(revision)
            if track.alerting and track.verdict.kind == "real" and track.verdict.confidence >= 0.6:
                out_alerts.append(self._rule_alert(track, now))
        return BookUpdate(verdicts=self.active(), alerts=out_alerts, events=events)

    # ------------------------------------------------------------------ свидетельства из правил и тревог

    def evidence_from_rule(self, v: Verdict, latest: Telemetry | None) -> list[SubjectEvidence]:
        """Разобрать выход правила сверки на свидетельства по утверждениям о полях.

        Args:
            v: Вердикт правила (для новых правил в `v.evidence` уже есть свидетельства).
            latest: Последний снимок.

        Returns:
            Свидетельства с привязкой к утверждениям.
        """
        w = self._v.weights.get(v.rule, 0.5)
        if v.evidence:  # правило само приложило свидетельства (thermal_balance, plan_vs_fact, cause_effect)
            subject = v.fields[0] if v.fields else v.rule
            return [
                SubjectEvidence(subject, e, list(v.fields), alerting=v.rule in DEFAULT_ALERTING_RULES) for e in v.evidence
            ]
        co2 = [f"co2_ppm.{k}" for k in (latest.co2_ppm if latest else {})]
        if v.rule in ("co2_consensus_high", "co2_consensus_rise"):
            out = []
            for subject in v.fields:
                others = [f for f in v.fields if f != subject]
                share = w / math.sqrt(max(len(others), 1))
                for o in others:
                    out.append(
                        SubjectEvidence(subject, Evidence(source=o, supports="real", weight=share, detail=f"{o} подтверждает рост", rule=v.rule), [subject])
                    )
            return out
        if v.rule == "co2_outlier":
            subject = v.fields[0]
            others = [f for f in co2 if f != subject] or ["other_sensors"]
            share = w / math.sqrt(len(others))
            return [
                SubjectEvidence(
                    subject,
                    Evidence(source=o, supports="false", weight=share, detail=f"{o} в норме, {subject} выбивается", rule=v.rule, kind_hint=v.kind if v.kind in FALSE_KINDS else "sensor_fault"),
                    [subject],
                )
                for o in others
            ]
        subject = "energy_balance" if v.rule in ("energy_balance",) else (v.fields[0] if v.fields else v.rule)
        source = {
            "energy_balance": "energy_balance",
            "battery_temp_plausibility": "battery_current",
            "device_status_vs_power": "power_meter",
            "pv_drop": "energy_balance",
            "co2_single_context": "context",
            "co2_pair_disagreement": "pair",
        }.get(v.rule, v.rule)
        if v.kind == "real":
            ev = Evidence(source=source, supports="real", weight=w, detail=v.explanation, rule=v.rule)
        elif v.kind in FALSE_KINDS:
            hint = "channel_fault" if v.rule == "device_status_vs_power" else ("drift" if v.rule == "battery_temp_plausibility" else v.kind)
            ev = Evidence(source=source, supports="false", weight=w, detail=v.explanation, rule=v.rule, kind_hint=hint)
        else:
            ev = Evidence(source=source, supports="neutral", weight=min(w, 0.2), detail=v.explanation, rule=v.rule)
        fields = list(v.fields) if v.rule == "energy_balance" else [subject]
        return [SubjectEvidence(subject, ev, fields, alerting=v.rule in DEFAULT_ALERTING_RULES and v.kind == "real")]

    def evidence_from_alert(self, a: Alert) -> list[SubjectEvidence]:
        """Свидетельства из тревог детекторов (физический предел, пара, шум, залипание, время, CUSUM)."""
        if a.field is None:
            return []
        w = self._v.weights
        if a.kind == "rate_limit":
            return [SubjectEvidence(a.field, Evidence(source="physics", supports="false", weight=w.get("rate_limit", 0.7), detail=a.message, rule="rate_limit", kind_hint="false_signal"), [a.field])]
        if a.kind == "pair_diff":
            return [SubjectEvidence(a.field, Evidence(source="pair", supports="false", weight=w.get("pair_diff", 0.6), detail=a.message, rule="pair_diff", kind_hint="sensor_fault"), [a.field])]
        if a.kind == "noise_loss":
            return [SubjectEvidence(a.field, Evidence(source="noise", supports="false", weight=w.get("noise_loss", 0.6), detail=a.message, rule="noise_loss", kind_hint="sensor_fault"), [a.field])]
        if a.kind == "freeze":
            return [SubjectEvidence(a.field, Evidence(source="flatline", supports="false", weight=w.get("freeze", 0.6), detail=a.message, rule="freeze", kind_hint="sensor_fault"), [a.field])]
        if a.kind == "time_monotonic":
            return [SubjectEvidence("ts", Evidence(source="timestamp", supports="false", weight=w.get("time_monotonic", 0.7), detail=a.message, rule="time_monotonic", kind_hint="channel_fault"), ["ts"])]
        if a.kind == "cusum":
            if a.field == "energy_balance":
                return [
                    SubjectEvidence(
                        "energy_balance",
                        Evidence(source="energy_balance", supports="real", weight=w.get("cusum", 0.6), detail=a.message, rule="cusum"),
                        ["energy_balance", "battery_soc_pct", "pv_power_w", "load_total_w"],
                        alerting=True,
                    )
                ]
            return [SubjectEvidence(a.field, Evidence(source="pair_cusum", supports="false", weight=w.get("cusum", 0.6), detail=a.message, rule="cusum", kind_hint="drift"), [a.field])]
        if a.kind == "actuator_delayed":
            return [SubjectEvidence(f"actuator:{a.field}", Evidence(source="late_effect", supports="false", weight=w.get("cause_effect", 0.7), detail=a.message, rule="cause_effect", kind_hint="channel_fault"), [f"actuator:{a.field}"])]
        return []

    # ------------------------------------------------------------------ внутреннее

    def _new_track(self, subject: str, now: datetime, fields: list[str]) -> _Track:
        """Создать утверждение (пока без свидетельств: вид `unknown`)."""
        verdict = Verdict(
            ts=now,
            kind="unknown",
            confidence=0.5,
            explanation="Данных для вывода пока недостаточно.",
            rule="",
            fields=fields,
            created_at=now,
            recheck_at=self._after(now),
        )
        track = _Track(subject=subject, verdict=verdict, fields=fields, last_claim_ts=now)
        self._tracks[subject] = track
        return track

    @staticmethod
    def _key(e: Evidence) -> tuple[str, str, str]:
        """Ключ свидетельства: один источник по одному правилу и стороне хранится один раз (обновляется)."""
        return (e.source, e.rule, e.supports)

    def _merge(self, track: _Track, e: Evidence, now: datetime) -> None:
        """Добавить или обновить свидетельство в утверждении."""
        track.evidence[self._key(e)] = e
        if e.supports == "false":
            track.last_false_ts = now

    def _effective(self, e: Evidence, now: datetime) -> float:
        """Вес свидетельства с учётом затухания по возрасту."""
        age = max(0.0, (now - (e.ts or now)).total_seconds())
        return float(e.weight * math.pow(0.5, age / self._v.evidence_half_life_s))

    def _recompute(self, track: _Track, now: datetime, events: list[BookEvent]) -> tuple[bool, Alert | None]:
        """Пересчитать вердикт утверждения.

        Returns:
            `(снять_с_учёта, тревога_пересмотра)`.
        """
        stale_claim = track.last_claim_ts is None or (now - track.last_claim_ts).total_seconds() > self._v.recheck_after_s
        watch_end = None if track.last_false_ts is None else track.last_false_ts.timestamp() + self._v.watch_after_false_s
        # выдохшиеся свидетельства отбрасываем; «ложные» держим до конца наблюдения
        for key, e in list(track.evidence.items()):
            keep_watch = e.supports == "false" and watch_end is not None and now.timestamp() < watch_end
            if self._effective(e, now) < self._v.min_weight and not keep_watch:
                del track.evidence[key]
        real_list: list[tuple[float, Evidence]] = []
        false_list: list[tuple[float, Evidence]] = []
        # «Контекст работ» лишь усиливает другие свидетельства: сам по себе он не создаёт вывода.
        only_context = all(e.rule == "context_work" for e in track.evidence.values() if e.supports != "neutral")
        for e in track.evidence.values():
            w = self._effective(e, now)
            if only_context and e.rule == "context_work":
                continue
            if e.supports == "real":
                real_list.append((w, e))
            elif e.supports == "false":
                false_list.append((w, e))
        if not real_list and not false_list and stale_claim:
            return True, None  # свидетельств нет, утверждение давно не подтверждается тревогами
        agg = self._v.aggregation
        z_sum = sum(w for w, _ in real_list) - sum(w for w, _ in false_list)
        p_real = sigmoid(agg.bias + agg.scale * z_sum) if (real_list or false_list) else 0.5
        track.p_real = p_real
        if p_real >= self._v.real_above:
            kind, conf = "real", min(p_real, self._v.max_confidence)
        elif p_real <= self._v.false_below:
            kind, conf = self._false_kind(false_list, track.verdict.kind), min(1.0 - p_real, self._v.max_confidence)
        else:
            kind, conf = "unknown", max(p_real, 1.0 - p_real)
        # Вердикт «ложный» не закрывается навсегда, но и не живёт вечно: после watch_after_false_s без
        # новых подтверждений ложного он снимается с наблюдения (новые свидетельства откроют его заново).
        if side_of(kind) == "false" and watch_end is not None and now.timestamp() >= watch_end and not real_list:
            return True, None
        rule = max(real_list + false_list, key=lambda x: x[0])[1].rule if (real_list or false_list) else ""
        prev = track.verdict
        new = prev.model_copy(
            update={
                "ts": now,
                "kind": kind,
                "confidence": round(conf, 4),
                "evidence": self._snapshot(track, now),
                "explanation": self._explain(kind, conf, real_list, false_list),
                "rule": rule,
                "fields": list(dict.fromkeys(track.fields or [track.subject])),
            }
        )
        revision: Alert | None = None
        # Вердикт, который давно не пересчитывался (дольше окна наблюдения), считается не определённым:
        # новое заключение по нему — не пересмотр, а новое.
        prev_stale = (now - prev.ts).total_seconds() > self._v.watch_after_false_s
        prev_kind = "unknown" if prev_stale else prev.kind
        prev_side, new_side = side_of(prev_kind), side_of(kind)
        recent = track.last_determinate_ts is not None and (now - track.last_determinate_ts).total_seconds() <= self._v.watch_after_false_s
        flipped_via_unknown = (
            prev_kind == "unknown" and new_side != "unknown" and track.last_side not in ("unknown", new_side) and recent
        )
        if prev_kind == "unknown" and kind != "unknown" and not flipped_via_unknown:
            new = new.model_copy(update={"created_at": now, "recheck_at": self._after(now), "status": "provisional"})
            events.append(BookEvent("created", new, reason=f"первое определённое заключение: {KIND_RU[kind]}"))
            track.checked_side = new_side
        elif flipped_via_unknown or (prev_side != "unknown" and new_side != "unknown" and prev_side != new_side):
            old_kind = prev.kind if prev.kind != "unknown" else track.last_kind
            refuted = prev.model_copy(update={"status": "refuted", "ts": now, "kind": old_kind})
            self.archive.append(refuted)
            self.revisions += 1
            new = new.model_copy(update={"id": new_id("vd"), "status": "provisional", "created_at": now, "recheck_at": self._after(now)})
            events.append(BookEvent("revised", new, previous=refuted, reason=f"{KIND_RU[old_kind]} → {KIND_RU[kind]}"))
            track.checked_side = new_side
            revision = self._revision_alert(track, refuted, new, now)
        elif kind != prev.kind and new_side != "unknown":
            events.append(BookEvent("reclassified", new, previous=prev, reason=f"{KIND_RU[prev.kind]} → {KIND_RU[kind]}"))
        elif new_side == "unknown" and prev_side != "unknown":
            events.append(BookEvent("reclassified", new, previous=prev, reason="уверенность упала, вывод снова не определён"))
        # плановая перепроверка (не для только что созданного или пересмотренного вердикта)
        if new.id == prev.id and prev.recheck_at is not None and now >= prev.recheck_at and new.created_at != now:
            if new_side != "unknown" and new_side == track.checked_side and new.status == "provisional":
                new = new.model_copy(update={"status": "confirmed"})
                events.append(BookEvent("confirmed", new, reason="перепроверка подтвердила вид вердикта"))
            track.checked_side = new_side
            new = new.model_copy(update={"recheck_at": self._after(now)})
        if new_side != "unknown":
            track.last_side, track.last_kind, track.last_determinate_ts = new_side, kind, now
        track.verdict = new
        return False, revision

    def _after(self, now: datetime) -> datetime:
        """Момент следующей перепроверки."""
        return now + timedelta(seconds=self._v.recheck_after_s)

    @staticmethod
    def _false_kind(false_list: list[tuple[float, Evidence]], previous: str = "unknown") -> str:
        """Вид «ложного» по подсказкам свидетельств (по суммарному весу) с гистерезисом.

        Прежний вид сохраняется, пока новый лидер не превосходит его более чем в 1.25 раза: иначе близкие по
        весу подсказки (дрейф против неисправности) заставляли бы вердикт мигать.
        """
        weights: dict[str, float] = {}
        for w, e in false_list:
            hint = e.kind_hint if e.kind_hint in FALSE_KINDS else "sensor_fault"
            weights[hint] = weights.get(hint, 0.0) + w
        if not weights:
            return "sensor_fault"
        best = max(weights, key=lambda k: weights[k])
        if previous in weights and previous != best and weights[best] < 1.25 * weights[previous]:
            return previous
        return best

    def _snapshot(self, track: _Track, now: datetime) -> list[Evidence]:
        """Свидетельства для публикации: вес показан с учётом затухания, самые весомые первыми."""
        out = [e.model_copy(update={"weight": round(min(1.0, self._effective(e, now)), 4)}) for e in track.evidence.values()]
        return sorted(out, key=lambda e: -e.weight)

    @staticmethod
    def _explain(kind: str, conf: float, real_list: list[tuple[float, Evidence]], false_list: list[tuple[float, Evidence]]) -> str:
        """Текстовое пояснение вердикта: что за, что против."""
        def top(items: list[tuple[float, Evidence]]) -> str:
            best = sorted(items, key=lambda x: -x[0])[:3]
            return "; ".join(f"{e.detail or e.rule} (вес {w:.2f})" for w, e in best)

        parts = [f"Вердикт: {KIND_RU.get(kind, kind)}, уверенность {conf:.0%}."]
        if real_list:
            parts.append(f"За «реальный»: {top(real_list)}.")
        if false_list:
            parts.append(f"За «ложный»: {top(false_list)}.")
        if not real_list and not false_list:
            parts.append("Свидетельств пока нет — только сам сигнал.")
        return " ".join(parts)

    def _revision_alert(self, track: _Track, old: Verdict, new: Verdict, now: datetime) -> Alert:
        """Тревога «пересмотр вердикта»."""
        level = AlertLevel.CRITICAL if side_of(old.kind) == "false" and new.kind == "real" else AlertLevel.WARNING
        return Alert(
            ts=now,
            level=level,
            kind="verdict_revised",
            field=track.subject,
            message=(
                f"Пересмотр вердикта по {track.subject}: «{KIND_RU[old.kind]}» → «{KIND_RU[new.kind]}» "
                f"(уверенность {new.confidence:.0%}); новые свидетельства: {self._brief(new)}"
            ),
            value=new.confidence,
            detector="verdict_book",
            confidence=new.confidence,
            details={"old_kind": old.kind, "new_kind": new.kind, "old_id": old.id, "new_id": new.id},
        )

    @staticmethod
    def _brief(v: Verdict) -> str:
        """Краткое перечисление главных свидетельств."""
        return ", ".join(f"{e.source}:{e.rule}" for e in v.evidence[:3])

    def _rule_alert(self, track: _Track, now: datetime) -> Alert:
        """Тревога по правилу, чей вердикт `real` сам является признаком проблемы."""
        v = track.verdict
        return Alert(
            ts=now,
            level=AlertLevel.WARNING,
            kind=v.rule or "crosscheck",
            field=track.subject,
            message=v.explanation,
            value=v.confidence,
            detector="crosscheck",
            confidence=v.confidence,
            details={"verdict_id": v.id, "sources": sorted(v.sources_for("real"))},
        )


def evidence_summary(v: Verdict) -> dict[str, Any]:
    """Краткое представление свидетельств вердикта для панели и журнала."""
    return {
        "real": [(e.source, e.rule, e.weight) for e in v.evidence if e.supports == "real"],
        "false": [(e.source, e.rule, e.weight) for e in v.evidence if e.supports == "false"],
        "sources_real": sorted(v.sources_for("real")),
        "sources_false": sorted(v.sources_for("false")),
    }


def context_evidence(latest: Telemetry | None, subjects: list[str], weight: float) -> list[SubjectEvidence]:
    """Свидетельство «контекст работ»: рост CO2 или температуры в момент пайки/станка ожидаем.

    Оно лишь немного повышает вероятность «реальный» и никогда не заглушает тревогу. Применяется
    только к уже открытым утверждениям (само по себе утверждения не создаёт).

    Args:
        latest: Последний снимок.
        subjects: Поля действующих утверждений.
        weight: Вес свидетельства (из `verdicts.weights.context_work`).

    Returns:
        Свидетельства `context` за «реальный».
    """
    if latest is None or weight <= 0:
        return []
    soldering = latest.solder_status.value == "running"
    machining = latest.cnc_status.value == "running" or latest.printer_status.value == "running"
    out: list[SubjectEvidence] = []
    for s in subjects:
        if s.startswith("co2_ppm.") and soldering:
            detail = "идёт пайка: рост CO2 ожидаем"
        elif s.startswith("temp_c") and (soldering or machining):
            detail = "идёт пайка/работа станка: рост температуры ожидаем"
        else:
            continue
        out.append(SubjectEvidence(s, Evidence(source="context", supports="real", weight=weight, detail=detail, rule="context_work"), [s]))
    return out
