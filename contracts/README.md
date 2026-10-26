# Контракт агента и БД/API — версия 1.0

Сервис хранения работает отдельно от агента. Локальный адрес разработки: `http://127.0.0.1:8080`. Версия контракта фиксирована в `event.schema.json`; проект реализации описан в `services/data_api/README.md`. Перечисленные HTTP-эндпоинты — контракт первой версии; работающий сервис ещё не реализован.

## Что сохраняется

Все снимки телеметрии со всеми полями и `null`, события, чат, каталог узлов, запросы и ответы Купола, ошибки и таймауты, решения и проверки агента, сообщения LLM и сообщения системных журналов. Неизвестные типы событий и поля не отбрасываются. API сервиса отдельно сохраняет каждый собственный HTTP-запрос и ответ, включая ошибки. Ключи и токены редактируются перед записью.

## Конверт события

```json
{
  "schema_version": "1.0",
  "event_id": "d10f6df4-7412-4dad-a141-50fc5b8898d5",
  "run_id": "team-session-20261026-01",
  "session_id": "ws-connection-01",
  "source": "dome",
  "channel": "websocket",
  "direction": "inbound",
  "event_type": "telemetry",
  "request_id": null,
  "node_id": null,
  "source_timestamp": "1791916543.2",
  "payload": {"type": "telemetry", "data": {"nodes": {}}},
  "metadata": {}
}
```

Направление всегда относительно агента: `outbound` — агент отправляет в Купол/LLM, `inbound` — получает; `internal` — внутренний журнал. `source` — источник записи (`dome`, `agent`, `llm`, `operator`, `system`, `import`), `channel` — транспорт или формат. `source_timestamp` сохраняет исходное представление времени без придумывания часового пояса. `ingested_at` назначает сервер в UTC. В `payload` сохраняется полный JSON, `raw_text` позволяет сохранить исходный текст или ошибочный JSON.

`event_id` задаётся отправителем, не меняется при повторной доставке. Одинаковое событие с тем же ID не записывается повторно; другой объект с тем же ID даёт 409. События хранятся неизменяемо, выдаются по серверному возрастающему `id`.

## HTTP вход и выход

| Метод и путь | Вход | Выход |
|---|---|---|
| GET `/health` | — | `{status, schema_version}` |
| POST `/v1/events` | Один конверт события | 201 `{id,event_id,created:true}`; повтор: 200 и `created:false` |
| POST `/v1/events/batch` | `{events:[конверт,...]}` до 1000 событий | `{items:[{id,event_id,created},...]}`; транзакция на весь пакет |
| GET `/v1/events` | `after_id=0`, `limit=100`, фильтры `run_id,session_id,source,channel,direction,event_type,node_id,request_id` | `{items:[событие с id и ingested_at,...],next_after_id}` |
| GET `/v1/runs` | — | `{items:[{run_id,event_count,first_ingested_at,last_ingested_at},...]}` |
| GET `/v1/telemetry/latest` | `run_id`, необязательный `node_id` | `{items:[{event_id,id,run_id,node_id,ingested_at,payload},...]}` — последнее полученное состояние каждого узла |
| GET `/v1/exchanges` | `run_id,session_id,request_id` | `{requests:[...],responses:[...],state}` |
| GET `/v1/http-exchanges` | `after_id`, `limit` | `{items:[краткие сведения HTTP,...],next_after_id}` |
| GET `/v1/http-exchanges/{id}` | Серверный ID записи HTTP | Полные вход/выход, заголовки без секретов, время, статус |

Общие ошибки: 401 — неверный токен сервиса; 404 — запись не найдена; 409 — повторный event_id с другим содержимым; 422 — неверный конверт/параметры; 500 — внутренняя ошибка/ошибка БД. Существующий формат ошибки FastAPI сохраняется в OpenAPI реализации. Сервер не подтверждает запись, если транзакция БД не завершилась.

Запись `control_response.success=true` подтверждает принятие команды сервером Купола. Фактическое выполнение подтверждается отдельной телеметрией и `verification` агента; БД не подменяет одно другим.

Полный внешний протокол Купола: [upstream-websocket.md](upstream-websocket.md). Разработка по веткам: [DEVELOPMENT.md](../docs/DEVELOPMENT.md).
