"""Пост-проверка действий, предложенных моделью: права из каталога и жёсткие правила hard_rules.yaml.

Вывод LLM не исполняется вслепую: каждое действие получает вердикт accepted/rejected с причинами.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from .context.catalog import NodeCatalog
from .schemas import ActionVerdict, AgentDecision, ProposedAction

HOLD_REASONS_NEED_OPERATOR = {"FILAMENT_RUNOUT"}
DANGEROUS = {("fire_suppression_01", "manual_start_zone"), ("access_control_01", "lockdown"),
             ("edge_compute_01", "reboot")}


class Ctx:
    """Всё, что нужно проверкам: телеметрия, журнал, лимиты и уже принятые действия плана."""

    def __init__(self, nodes: dict, journal: list[dict], limits: dict, catalog: NodeCatalog):
        self.nodes = nodes
        self.journal = journal
        self.limits = limits
        self.catalog = catalog
        self.accepted: list[ProposedAction] = []

    def node(self, node_id: str) -> dict:
        return self.nodes.get(node_id) or {}

    def planned(self, node_id: str, action: str) -> list[ProposedAction]:
        return [a for a in self.accepted if a.node_id == node_id and a.action == action]


def _line_no(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("line")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(ts: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        return None


# --- Проверки. Возвращают текст причины отказа или None ---

def resume_requires_cause_cleared(a: ProposedAction, c: Ctx) -> str | None:
    if a.action != "resume":
        return None
    node = c.node(a.node_id)
    if (node.get("lockout_remaining_s") or 0) > 0:
        return f"станок в блокировке ещё {node['lockout_remaining_s']} с"
    reason = node.get("hold_reason")
    if reason in HOLD_REASONS_NEED_OPERATOR:
        return f"удержание {reason} снимается только после действия Оператора"
    if reason and str(reason).startswith("IL_"):
        il = (c.node("dome_automation_01").get("interlocks") or {}).get(reason) or {}
        released_in_plan = any(_il_id(x.value) == reason for x in c.planned("dome_automation_01", "reset_interlock"))
        if il.get("state") == "TRIPPED" and not released_in_plan:
            return f"интерлок {reason} ещё TRIPPED и в плане нет reset_interlock — resume будет пустым"
    return None


def resume_attempts_limit(a: ProposedAction, c: Ctx) -> str | None:
    if a.action != "resume":
        return None
    attempts = c.node(a.node_id).get("resume_attempts") or 0
    if attempts >= c.limits.get("resume_attempts_max", 2):
        return f"уже {attempts} пустых попыток resume — следующая даст ALARM на 20 минут"
    return None


def printer_bed_clear(a: ProposedAction, c: Ctx) -> str | None:
    if a.node_id == "printer_3d_01" and a.action == "start_job" and c.node(a.node_id).get("bed_clear") is False:
        return "bed_clear=false: на столе деталь, будет BED_COLLISION"
    return None


def start_job_state(a: ProposedAction, c: Ctx) -> str | None:
    if a.action != "start_job":
        return None
    node = c.node(a.node_id)
    if node.get("state") in ("PRINTING", "RUNNING", "PAUSED", "BUSY", "HOMING"):
        return f"станок в состоянии {node['state']} — новое задание запускать нельзя"
    name = a.value.get("file_name") if isinstance(a.value, dict) else a.value
    files = node.get("files")
    if isinstance(files, list) and files:
        names = {f.get("name") if isinstance(f, dict) else f for f in files}
        if name not in names:
            return f"файла {name!r} нет в списке files станка"
    return None


def box_lock_during_print(a: ProposedAction, c: Ctx) -> str | None:
    if a.node_id != "thermal_insulation_01" or a.action != "set_lock":
        return None
    unlocking = isinstance(a.value, dict) and a.value.get("locked") is False
    printer = c.node("printer_3d_01")
    if unlocking and printer.get("state") in ("PRINTING", "PAUSED"):
        return f"принтер в состоянии {printer['state']} (progress {printer.get('progress_pct')}%) — бокс не открывать"
    return None


def line_switching(a: ProposedAction, c: Ctx) -> str | None:
    if a.node_id != "smart_panel_01" or a.action not in ("line_on", "line_off", "reset_protection"):
        return None
    n = _line_no(a.value)
    if n is None or not 1 <= n <= 8:
        return "номер линии должен быть 1..8"
    lines = c.node("smart_panel_01").get("lines") or []
    line = next((ln for ln in lines if isinstance(ln, dict) and ln.get("line") == n), {})
    if (line.get("lockout_remaining_s") or 0) > 0:
        return f"линия {n} заблокирована ещё {line['lockout_remaining_s']} с"
    if a.action == "line_on" and line.get("state") in ("TRIPPED", "RCD_TRIP"):
        return f"защита линии {n} сработала ({line['state']}) — нужен reset_protection, а не line_on"
    window = timedelta(seconds=c.limits.get("line_switch_window_s", 120))
    now = datetime.now().astimezone()
    recent = sum(1 for r in c.journal
                 if r.get("actor") == "agent" and r.get("node_id") == "smart_panel_01"
                 and r.get("action") in ("line_on", "line_off") and _line_no(r.get("value")) == n
                 and (ts := _parse_ts(r.get("ts"))) and now - ts <= window)
    planned = sum(1 for x in c.accepted
                  if x.node_id == "smart_panel_01" and x.action in ("line_on", "line_off") and _line_no(x.value) == n)
    if recent + planned + 1 > c.limits.get("line_switch_max", 3):
        return f"линия {n}: превышен лимит переключений ({recent + planned} за окно) — блокировка на 5 минут"
    return None


def _il_id(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("id") or value.get("interlock")
    return value if isinstance(value, str) else None


def interlock_reset_requires_trust(a: ProposedAction, c: Ctx) -> str | None:
    if a.node_id != "dome_automation_01" or a.action != "reset_interlock" or _il_id(a.value) != "IL_CO2":
        return None
    plc = c.node("dome_automation_01")
    primary = plc.get("primary_co2_sensor") or "climate_sensor_01"
    trust = plc.get("data_trust") or {}
    for x in c.planned("dome_automation_01", "set_primary_sensor"):
        primary = x.value if isinstance(x.value, str) else primary
    distrusted = any(isinstance(x.value, dict) and x.value.get("node") == primary
                     for x in c.planned("dome_automation_01", "set_data_trust"))
    distrusted |= trust.get(f"{primary}.co2_ppm") in ("UNRELIABLE", "FAILED") or trust.get(f"{primary}.*") in ("UNRELIABLE", "FAILED")
    co2 = c.node(primary).get("co2_ppm")
    if not distrusted and isinstance(co2, (int, float)) and co2 > 2000:
        return f"основной датчик {primary} показывает {co2} ppm и не помечен ненадёжным — сначала set_data_trust/set_primary_sensor"
    return None


def block_interlock_limit(a: ProposedAction, c: Ctx) -> str | None:
    if a.node_id != "dome_automation_01" or a.action != "block_interlock":
        return None
    minutes = a.value.get("minutes") if isinstance(a.value, dict) else None
    limit = c.limits.get("block_interlock_max_min", 30)
    if not isinstance(minutes, (int, float)) or minutes > limit:
        return f"block_interlock требует minutes ≤ {limit}"
    return None


def no_upload(a: ProposedAction, c: Ctx) -> str | None:
    if a.action == "upload_file":
        return "агент не загружает G-code сам: только файлы организаторов"
    return None


def no_duplicates(a: ProposedAction, c: Ctx) -> str | None:
    key = json.dumps(a.value, sort_keys=True, ensure_ascii=False, default=str)
    for x in c.accepted:
        if x.node_id == a.node_id and x.action == a.action and \
                json.dumps(x.value, sort_keys=True, ensure_ascii=False, default=str) == key:
            return "дубль команды в одном плане"
    return None


def operator_message_length(a: ProposedAction, c: Ctx) -> str | None:
    if a.node_id == "meeting_01" and a.action == "post_message":
        text = a.value.get("text") if isinstance(a.value, dict) else a.value
        if not text or len(str(text)) > c.limits.get("message_max_chars", 4000):
            return "пустое или слишком длинное сообщение"
    return None


def dangerous_actions(a: ProposedAction, c: Ctx) -> str | None:
    if (a.node_id, a.action) in DANGEROUS and a.priority > 1:
        return "опасное действие допускается только как первоочередное (priority=1) при подтверждённой угрозе"
    if a.node_id == "smart_panel_01" and a.action == "line_off" and _line_no(a.value) == 7:
        return "аварийное освещение (линия 7) не отключается"
    return None


CHECKS: dict[str, Callable[[ProposedAction, Ctx], str | None]] = {
    f.__name__: f for f in (
        resume_requires_cause_cleared, resume_attempts_limit, printer_bed_clear, start_job_state,
        box_lock_during_print, line_switching, interlock_reset_requires_trust, block_interlock_limit,
        no_upload, no_duplicates, operator_message_length, dangerous_actions)
}


def validate_actions(actions: list[ProposedAction], catalog: NodeCatalog, telemetry_nodes: dict,
                     journal: list[dict], hard_rules: dict) -> list[ActionVerdict]:
    rules = hard_rules.get("rules", [])
    active = [(r["id"], CHECKS[r["check"]]) for r in rules if r.get("check") in CHECKS]
    ctx = Ctx(telemetry_nodes, journal, hard_rules.get("limits", {}), catalog)
    verdicts = []
    for action in sorted(actions, key=lambda x: x.priority):
        reasons = []
        if not catalog.get(action.node_id):
            reasons.append(f"узел {action.node_id} отсутствует в каталоге")
        elif catalog.is_operator_action(action.node_id, action.action):
            reasons.append(f"{action.action} — физическое действие Оператора: просить через meeting_01")
        else:
            canonical = catalog.canonical_action(action.node_id, action.action)
            if canonical is None:
                allowed = catalog.get(action.node_id).get("controls", [])
                reasons.append(f"действие {action.action} не разрешено для {action.node_id}; разрешено: {allowed}")
            else:
                action = action.model_copy(update={"action": canonical})
        if not reasons:
            for rule_id, check in active:
                problem = check(action, ctx)
                if problem:
                    reasons.append(f"{rule_id}: {problem}")
        accepted = not reasons
        if accepted:
            ctx.accepted.append(action)
        verdicts.append(ActionVerdict(action=action, accepted=accepted, reasons=reasons))
    return verdicts


def check_classification(decision: AgentDecision, telemetry_age_s: float | None, stale_s: float) -> list[str]:
    """Согласованность разметки воздействия, которую делает модель (код сам ничего не классифицирует).

    Возвращает замечания для журнала и панели. Достоверность по устаревшему кадру не подтверждается:
    authenticity сбрасывается в unknown.
    """
    issues = []
    if telemetry_age_s is not None and telemetry_age_s > stale_s and decision.authenticity != "unknown":
        issues.append(f"authenticity={decision.authenticity} по кадру {telemetry_age_s:.0f} с давности — заменено на unknown")
        decision.authenticity = "unknown"
    if decision.impact_kind == "false" and decision.authenticity == "real":
        issues.append("impact_kind=false при authenticity=real: подтверждённый сигнал не может быть ложным")
    if decision.false_alarm is True and decision.authenticity == "real":
        issues.append("false_alarm=true противоречит authenticity=real")
    if decision.false_alarm is False and decision.authenticity == "suspected_false":
        issues.append("false_alarm=false противоречит authenticity=suspected_false")
    if decision.impact_kind == "cascade" and len(decision.causal_chain) < 2:
        issues.append("impact_kind=cascade без цепочки из двух и более звеньев")
    if decision.authenticity != "unknown" and not decision.evidence:
        issues.append(f"authenticity={decision.authenticity} без показаний в evidence")
    return issues
