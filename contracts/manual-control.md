# Ручное управление Куполом (веб-интерфейс) — версия 1.0

Интерфейс не подключается к Куполу: соединение с ключом команды одно, команды отправляет агент
(`agent/src/kupol/dome/manual.py`) через свой Gate. Тот же Gate и те же правила, что у команд LLM-агента.

1. Узнать запуск и сессию агента: `GET /v1/runs` (или заданный `DATA_API_RUN_ID`), сессия — `session_id`
   последнего `auth_response` этого запуска (`GET /v1/events?run_id=…&event_type=auth_response`).
2. Отправить намерение `POST /v1/controls/validate`:

```json
{"event_id": "<uuid>", "run_id": "<запуск агента>", "session_id": "<сессия агента>", "source": "operator",
 "channel": "websocket", "direction": "internal", "event_type": "control_intent", "request_id": "ui-1",
 "node_id": "dome_automation_01",
 "payload": {"type": "control", "request_id": "ui-1", "node_id": "dome_automation_01",
             "action": "set_primary_sensor", "value": "climate_sensor_02"},
 "metadata": {"operator": "имя", "reason": "почему"}}
```

   Ответ — проверка сервера (`validation.status`, `reasons`: каталог, права, значение, свежесть состояния).
   Отклонённое сервером агент не исполняет.
3. Агент забирает принятые намерения своего запуска (`GET /v1/controls?actor=operator&validation=accepted`),
   записывает решение `source=operator` и отправляет команду через Gate: роль, каталог, политика `dome.yaml`,
   значение, защитные правила. Без `DOME_ALLOW_CONTROL=1` команда не уходит (`dry_run`). Намерения старше
   60 с не исполняются (`skipped`).
4. Итог — событие `control_dispatch` (`GET /v1/events?run_id=…&event_type=control_dispatch`):
   `payload = {intent_event_id, status: sent|dry_run|rejected|failed|skipped, reason, decision_seq, action_seq, response}`,
   `request_id` — команды в Купол. Ответ Купола — `control_response` с тем же `request_id`, проверка эффекта —
   `agent_verification`.
