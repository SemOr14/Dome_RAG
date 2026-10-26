"""Доска рекомендаций: связывает агентов, предохранитель, журнал и панель.

Рекомендации с командами проходят через ``Guard.submit``: безопасные команды класса
``auto`` выполняются сразу (рекомендация получает статус ``auto_applied``), команды
``needs_human`` попадают в очередь подтверждения предохранителя, а рекомендация ждёт
кнопки «Принять» или «Отклонить». Принятие рекомендации подтверждает её команды
в предохранителе; единственная точка подтверждения команд — ``Guard``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import (
    CommandResult,
    CommandStatus,
    JournalEntry,
    Recommendation,
    RecommendationStatus,
)

DEDUP_WINDOW_S = 60.0


class RecommendationBoard:
    """Хранилище и обработчик рекомендаций агентов (потокобезопасное)."""

    def __init__(
        self,
        guard: Guard,
        journal: Journal,
        clock: Callable[[], datetime],
        ttl_s: float = 600.0,
    ) -> None:
        """Args:
        guard: Предохранитель команд.
        journal: Журнал решений.
        clock: Источник времени.
        ttl_s: Через сколько секунд нерассмотренная рекомендация истекает.
        """
        self._guard = guard
        self._journal = journal
        self._clock = clock
        self._ttl = ttl_s
        self._lock = threading.RLock()
        self._recs: list[Recommendation] = []
        self._cmd_ids: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ добавление

    def add(self, rec: Recommendation) -> Recommendation | None:
        """Принять рекомендацию агента: выполнить допустимые команды, записать в журнал.

        Args:
            rec: Рекомендация (команды ещё не отправлены).

        Returns:
            Сохранённая рекомендация либо ``None``, если такая же уже есть в окне дедупликации.
        """
        with self._lock:
            now = self._clock()
            if self._is_duplicate(rec, now):
                return None
            notes: list[str] = []
            pending_ids: list[str] = []
            applied = 0
            for cmd in rec.commands:
                res = self._guard.submit(cmd, rec.agent)
                if res.status is CommandStatus.PENDING:
                    pending_ids.append(cmd.id)
                elif res.status is CommandStatus.ACCEPTED:
                    applied += 1
                else:
                    notes.append(f"{cmd.name} не выполнена: {res.message}")
            if rec.commands:
                if pending_ids:
                    rec.needs_human = True
                    rec.status = RecommendationStatus.PENDING
                elif applied:
                    rec.needs_human = False
                    rec.status = RecommendationStatus.AUTO_APPLIED
                else:
                    rec.needs_human = False
                    rec.status = RecommendationStatus.INFO
            else:
                rec.status = RecommendationStatus.PENDING if rec.needs_human else RecommendationStatus.INFO
            if notes:
                rec.rationale = (rec.rationale + "\n" if rec.rationale else "") + "; ".join(notes)
            self._cmd_ids[rec.id] = pending_ids
            self._recs.append(rec)
            self._journal_rec(rec, "рекомендация")
            return rec

    def _is_duplicate(self, rec: Recommendation, now: datetime) -> bool:
        """Такая же рекомендация (агент и заголовок) уже ждёт решения или была недавно."""
        for old in reversed(self._recs):
            same = old.agent == rec.agent and old.title == rec.title
            if same and old.status is RecommendationStatus.PENDING:
                return True
            if same and (now - old.ts).total_seconds() <= DEDUP_WINDOW_S:
                return True
        return False

    # ------------------------------------------------------------------ решения человека

    def accept(self, rec_id: str, who: str = "human") -> Recommendation | None:
        """Принять рекомендацию: подтвердить её команды в предохранителе.

        Args:
            rec_id: Идентификатор рекомендации.
            who: Кто принял (в журнал).

        Returns:
            Обновлённая рекомендация либо ``None``, если её нет или она уже обработана.
        """
        with self._lock:
            rec = self._find_pending(rec_id)
            if rec is None:
                return None
            results: list[CommandResult] = [self._guard.approve(cid, who) for cid in self._cmd_ids.get(rec.id, [])]
            failed = [r for r in results if r.status not in (CommandStatus.ACCEPTED,)]
            rec.status = RecommendationStatus.ACCEPTED
            if failed:
                rec.rationale += ("\n" if rec.rationale else "") + "; ".join(f"не выполнено: {r.message}" for r in failed)
            self._cmd_ids[rec.id] = []
            self._journal_rec(rec, f"принято человеком ({who})", actor=who)
            return rec

    def reject(self, rec_id: str, who: str = "human", reason: str = "") -> Recommendation | None:
        """Отклонить рекомендацию и её команды.

        Args:
            rec_id: Идентификатор рекомендации.
            who: Кто отклонил.
            reason: Причина.

        Returns:
            Обновлённая рекомендация либо ``None``.
        """
        with self._lock:
            rec = self._find_pending(rec_id)
            if rec is None:
                return None
            for cid in self._cmd_ids.get(rec.id, []):
                self._guard.reject(cid, who, reason)
            rec.status = RecommendationStatus.REJECTED
            self._cmd_ids[rec.id] = []
            self._journal_rec(rec, f"отклонено человеком ({who})" + (f": {reason}" if reason else ""), actor=who)
            return rec

    def accept_all(self, who: str) -> int:
        """Принять все ожидающие рекомендации (для headless-симуляции с ``--auto-approve``).

        Args:
            who: Кто подтверждает (в журнал; в симуляции — ``sim-auto-approve``).

        Returns:
            Сколько рекомендаций принято.
        """
        with self._lock:
            ids = [r.id for r in self._recs if r.status is RecommendationStatus.PENDING]
            return sum(1 for rid in ids if self.accept(rid, who) is not None)

    def expire(self) -> int:
        """Пометить истекшими рекомендации, на которые не ответили.

        Истекает рекомендация старше ``ttl_s`` и рекомендация, все команды которой уже
        исчезли из очереди предохранителя (срок подтверждения вышел): принять её было бы нечем.

        Returns:
            Сколько рекомендаций истекло.
        """
        with self._lock:
            now = self._clock()
            n = 0
            for rec in self._recs:
                if rec.status is not RecommendationStatus.PENDING:
                    continue
                ids = self._cmd_ids.get(rec.id, [])
                commands_gone = bool(rec.commands) and bool(ids) and not any(self._guard.is_pending(c) for c in ids)
                if (now - rec.ts).total_seconds() > self._ttl or commands_gone:
                    rec.status = RecommendationStatus.EXPIRED
                    self._cmd_ids[rec.id] = []
                    self._journal_rec(rec, "срок рассмотрения истёк", actor="board")
                    n += 1
            return n

    # ------------------------------------------------------------------ чтение

    def pending(self) -> list[Recommendation]:
        """Рекомендации, ожидающие решения человека (старые первыми)."""
        with self._lock:
            return [r for r in self._recs if r.status is RecommendationStatus.PENDING]

    def recent(self, n: int = 20) -> list[Recommendation]:
        """Последние рекомендации, новые первыми."""
        with self._lock:
            return list(reversed(self._recs[-n:]))

    def all(self) -> list[Recommendation]:
        """Все рекомендации прогона в порядке появления."""
        with self._lock:
            return list(self._recs)

    # ------------------------------------------------------------------ внутреннее

    def _find_pending(self, rec_id: str) -> Recommendation | None:
        """Ожидающая рекомендация по id."""
        for r in self._recs:
            if r.id == rec_id and r.status is RecommendationStatus.PENDING:
                return r
        return None

    def _journal_rec(self, rec: Recommendation, event_prefix: str, actor: str | None = None) -> None:
        """Запись о рекомендации в журнал по форме п. 7.3 регламента."""
        top = rec.hypotheses[0] if rec.hypotheses else None
        self._journal.log(
            JournalEntry(
                ts=self._clock(),
                actor=actor or rec.agent,
                event=f"{event_prefix}: {rec.title}",
                cross_check=top.safe_check if top else "",
                hypothesis=f"{top.cause} ({top.probability:.0%})" if top else "",
                impact=f"приоритет {rec.priority}",
                action="; ".join(f"{c.name} {c.args}" for c in rec.commands) or rec.rationale[:300],
                executor="Штаб" if rec.commands else "человек",
                result=("резервный сценарий" if rec.used_fallback else "модель/код"),
                status=rec.status.value,
                kind="recommendation",
                data={"recommendation_id": rec.id, "agent": rec.agent, "used_fallback": rec.used_fallback},
            )
        )
