# Интерфейс с агентом: промпт, решение, управление

Сервис хранения не запускает LLM и не отправляет команды. Алгоритмы и создание промпта
остаются у разработчика agent/. Здесь фиксируется доставка полного входа/выхода через
уже действующий конверт contracts/event.schema.json. RAG не используется этим сервисом.

## Промпт

Агент получает текущие состояния через GET /v1/telemetry/latest?run_id=...,
а исходный конверт с metadata.observed_at/source_timestamp — GET /v1/events/{event_id}.
ingested_at означает получение БД, а не новое измерение. Исторические события нельзя
представлять как текущие показания.

Рекомендуемые входные разделы промпта:

1. Задача и ограничения: агент предлагает действия; выполняет их отдельный Gate.
2. Наблюдения: node_id, исходные значения, timestamp, observed_at, online/status,
   ссылки на event_id. Неизвестные/null значения не заменяются догадками.
3. Каталог разрешённых действий из текущей сессии, ограничения значений.
4. Последнее решение, отправленные команды, ответы и результаты verification.
5. Требование вернуть JSON решения в формате ниже. Принятие сервером не равно выполнению.

Перед вызовом модели записывать событие source=agent, channel=llm, direction=outbound,
event_type=llm_request, с полным payload={messages,model,parameters,response_schema}.
Ответ: source=llm, channel=llm, direction=inbound, event_type=llm_response,
payload={model,raw_response,parsed_output,usage,error}. Один run_id/session_id/request_id,
разные event_id. Секреты не включать в промпт; сохранять весь ответ, включая ошибочный JSON.
Номер запроса передавать с исходным JSON-типом.

## Прогнозы и подозрения агента

Два дополнительных типа событий source=agent, direction=internal (агентная система, `agent/src/kupol/dome/`):

- `forecast` — раз в минуту: `payload={items:[{id,label,node_id,param,unit,now,trend,rate_per_h,fit,reaches:[{threshold,name,eta_h}],by_power}],dome_time,basis}`.
  Время до порога (заряд АКБ до 25 % и до 0, топливо, температура, CO2, влажность) в часах реального времени по линейному
  тренду; для АКБ ещё оценка по мощности разряда (`by_power`). Это экстраполяция, а не измерение.
- `agent_suspicion` — подозрительное наблюдение «на будущее», не более одного за решение модели:
  `payload={id,text,node_id,param,why,check,status,dome_time,decision_id}`. Те же записи хранятся в `state/suspicions.jsonl`,
  возвращаются модели в промпте (24 часа) и не создают команд в Купол.

## Структура результата

Рекомендуемый payload события source=agent, direction=internal, event_type=decision:

```json
{
  "decision_id": "stable-decision-id",
  "observation_event_ids": ["uuid-of-observed-event"],
  "summary": "Наблюдаемая проблема",
  "hypotheses": [{"description": "Проверяемая причина", "confidence": null}],
  "proposed_controls": [
    {"node_id": "smart_panel_01", "action": "line_off", "value": 8}
  ],
  "needs_operator": false,
  "verification_plan": [{"node_id": "smart_panel_01", "fields": ["lines"],
                         "expected": "Уменьшение нагрузки", "timeout_s": 10}],
  "status": "proposed"
}
```

Этот payload — рекомендация интеграции, не новый обязательный общий контракт:
сервис сохраняет и другие структурированные решения без потери неизвестных полей.
Ошибочный результат модели записывается как llm_response/parse_error, командой не становится.

## Команды и результат

Для каждого proposed_control создать отдельный control_intent и вызвать
POST /v1/controls/validate. В metadata передать decision_event_id/decision_id.
Для человека source=operator, для агента source=agent. Все отклонённые попытки тоже
сохраняются. Ответ содержит validation.accepted/status/reasons и checked_at.

После допуска Gate агента сам отправляет команду и записывает отдельный outbound control,
ссылки metadata.intent_event_id и metadata.decision_event_id. Ответ Купола сохраняется
отдельным inbound control_response. GET /v1/controls/{event_id} показывает:

- validation.status: принято/отклонено локальной проверкой;
- upstream_status: accepted/rejected/pending/ambiguous по ответу Купола;
- effect_verified=false: выполнение нельзя утверждать без проверки агента.

Проверку эффекта записывать как verification со ссылками на решение, команду, ответ
и новую телеметрию; proposed/accepted/rejected/verified — разные этапы.
Общая запись: POST /v1/events или атомарный POST /v1/events/batch.
Все сетевые повторы используют прежние event_id, подтверждение — после COMMIT.
