# БД и HTTP API Купола

Python 3.11+, FastAPI, SQLite. Код только в services/data_api/. Агент остаётся владельцем
WebSocket и отправки команд. Сервис не импортирует agent/, не подключается к оборудованию.
Агент разрабатывается отдельно; его ветка здесь не объединяется.
RAG не входит в текущую задачу. БД создаётся пустой, история автоматически не импортируется.

## Запуск

Из корня репозитория:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r services/data_api/requirements-dev.txt
cd services/data_api
export DATA_API_DB=../../state/data-api.sqlite3
export DATA_API_TOKEN='your-local-service-token'
uvicorn data_api.app:create_app --factory --host 127.0.0.1 --port 8080
```

PowerShell: `.venv\Scripts\Activate.ps1`, `$env:DATA_API_TOKEN='...'`.
Для всех запросов: `Authorization: Bearer <DATA_API_TOKEN>`. Без переменной токен не нужен
(локальная разработка на loopback). OpenAPI `/openapi.json`, Swagger `/docs`; при включённом
токене документацию также получать авторизованным запросом или через авторизующий proxy.
Не размещайте токен сервиса в открытом frontend.

`DATA_API_REDACT_VALUES` — дополнительные секреты через запятую для удаления из текстов.
`DATA_API_CORS_ORIGINS` — точные origin веб-интерфейса через запятую, по умолчанию CORS выключен.
Это настройка доступа к HTTP API.

## Хранение и гарантии

- events: неизменяемые конверты, полные payload/raw_text/metadata и исходное время.
  Неизвестные поля внутри payload/metadata и неизвестные типы событий сохраняются.
  Поля конверта валидируются общей contracts/event.schema.json, без изменения контракта.
  null, bool, числа, строки и массивы не приводятся к другим типам. NaN/Infinity и
  повторяющиеся ключи JSON явно отклоняются, без молчаливой потери значения.
- node_snapshots: каждый узел каждого telemetry, включая повторные состояния и null,
  со ссылкой на событие. Поддерживаются payload.data.nodes, payload.nodes и отдельный
  payload с envelope.node_id. Последнее состояние определяется серверным id получения.
- control_checks: accepted/rejected, причины, checked_at, ID контекста для каждого
  control/control_intent/исторического action человека или агента. Отказ проверки не мешает
  сохранить исходную попытку. control_receipts сохраняет все ответы Купола.
- http_exchanges: полные запросы/ответы, query, заголовки, тела, UTC-время, статусы,
  включая 401/404/405/409/422/500, документацию и чтение аудита. Без усечения тел.
- schema_migrations: версия SQL; migrations/001_initial.sql применяется автоматически.
  WAL, foreign_keys, synchronous=FULL, BEGIN IMMEDIATE, отдельные соединения;
  UPDATE/DELETE журналов блокируются SQL-триггерами.

Ключи, токены, Authorization/Cookie и пароли редактируются **до записи**, также во вложенных
JSON, raw_text, текстах, query. Неизвестный секрет в произвольной фразе без метки распознать
невозможно: задавайте DATA_API_REDACT_VALUES и редактируйте на стороне отправителя.
raw_text сохраняет исходный текст кроме секретов; текстовый JSON с секретом может быть
пересериализован. Невалидные UTF-8 байты HTTP представлены как `\xNN`, без исполнения.

event_id с одинаковым каноническим редактированным содержимым — повтор (created=false);
другой объект — 409. Порядок ключей не влияет. Отсутствие optional-поля и null различаются,
schema_version=1.0 подставляется. `1`, `1.0`, `"1"` хранятся и коррелируются раздельно.
Исходные пробелы и порядок ключей сохраняйте через raw_text. Смена только скрытого секрета
не создаёт конфликт, поскольку сравнение выполняется после редактирования.

Пакет фиксируется одной транзакцией вместе со снимками и проверками. Ошибка/конфликт
откатывает весь пакет. HTTP-ответ выдаётся после фиксации аудита; при отказе аудита — 503,
даже если события уже записались. Повторять с теми же event_id. Если сам SQLite недоступен,
физически сохранить аудит сбоя нельзя; положительное подтверждение не выдаётся.
Локальные SQLite/WAL и импортированные данные не коммитятся.

## Методы

Базовые пути и конверт: [контракт](../../contracts/README.md),
[Schema](../../contracts/event.schema.json). Реализованы все документированные пути:

| Метод | Назначение |
|---|---|
| GET /health | status, schema_version |
| POST /v1/events | Конверт; 201 новая запись, 200 повтор |
| POST /v1/events/batch | {events:[...]}, 1–1000, атомарно, 200 |
| GET /v1/events | after_id, limit=1..1000, фильтры общего контракта |
| GET /v1/events/{event_id} | Полный конверт с id/ingested_at |
| GET /v1/runs | Сводка; limit, after_run_id |
| GET /v1/telemetry/latest | run_id обязателен; node_id, limit, after_node_id |
| GET /v1/exchanges | run_id, session_id, request_id обязательны |
| GET /v1/http-exchanges | Краткий список, after_id, limit |
| GET /v1/http-exchanges/{id} | Полный редактированный обмен |
| POST /v1/controls/validate | Сохранить control_intent и вернуть проверку |
| GET /v1/controls | after_id, limit, run_id, actor, validation |
| GET /v1/controls/{event_id} | Команда, проверка, ответы, upstream_status |

request_id в query — JSON-литерал: `7`, `7.0` или `"7"`; в /events допустим null.
Пример: `params={"run_id":"r","session_id":"s","request_id":json.dumps(request_id)}`.
node_id в /events ищет также вложенные снимки. after_id исключительный: продолжать с
next_after_id до пустого items. Для runs/telemetry аналогичны их строковые курсоры.
Обмен: complete/pending/orphan_response/not_found/ambiguous. Повторный request_id внутри
одной сессии неоднозначен; другой run/session и HTTP/LLM-каналы не смешиваются.
Дополнительные методы описаны здесь и в OpenAPI для согласования на review;
общие contracts/ и конверт 1.0 не менялись.

## Управляющие воздействия

Для каждой команды независимо сохраняются:

1. validation.status=accepted/rejected: локальная проверка и причины, canonical_action,
   context_event_ids, checked_at, rules_version.
2. upstream_status=accepted/rejected: строго boolean success ответа Купола;
   pending, если ответа нет, ambiguous при противоречии/неоднозначной корреляции.
   Для исторического action.success принятие сохраняется сразу.
3. effect_verified=false: принятие не доказывает выполнение. Эффект подтверждает агент
   отдельным verification и телеметрией; сервис не придумывает результат.

Для agent/operator одинаково проверяются авторизованная team-сессия, её describe_nodes,
право действия/алиас, JSON-типы и диапазоны из 01_API.md, доступность и свежесть состояния.
Агенту запрещены operator_controls. Неописанные физические действия человека сохраняются,
но локально отклоняются с parameter_rule_unavailable: форматов этих параметров в API нет.
Фактический ответ Купола сохраняется независимо от результата локальной проверки.

В telemetry обязательно передавать metadata.observed_at — ISO 8601 UTC с offset,
фактическое время наблюдения, не повторной доставки. Получение и наблюдение — не старше
30 с. Сравниваются timestamp кадра и updated_at узла в исходных часах Купола.
UNKNOWN, offline, старое/неизвестное измерение не дают допуск. Контекст отделён по run/session.

Проверяются каталог файлов, занятый стол принтера, удержания/блокировки, причина resume,
наладка и перемещения, защита линий, открытие бокса при печати, соответствие протокола
каналу связи, ID интерлока, MPPT-каналы и ёмкость бака. Лимиты записанных отправок:
5/с, чат 10/10 с, переключения линии 3/120 с. Неизвестные абсолютные границы принтера — отказ.
Это консервативная предварительная проверка, не замена Gate агента, диагностики причин,
серверных интерлоков и физической проверки. Подготовку G-code организаторами API доказать
не может. Проверка не является отправкой или бессрочным разрешением.

## Доставка событий: интерфейс разработчику агента

1. Новый run_id на запуск и session_id на каждое WS-соединение. UUID event_id создавать
   до помещения события в durable-очередь, при повторе не менять.
2. Писать все WS входы/выходы, auth, неизвестные кадры, JSON-ошибки (payload=null/raw_text),
   reconnect/timeout, решения, чат, логи и полный обмен с LLM. direction относительно агента;
   request_id без str()/int(). Исходный JSON целиком в payload, свои дополнения в metadata.
3. Доставить auth_response, describe_nodes_response, telemetry с observed_at.
4. До команды вызвать POST /v1/controls/validate: обычный конверт source=agent/operator,
   direction=internal, event_type=control_intent, payload={type:control,request_id,node_id,
   action,value?}. request_id/node_id конверта соответствуют payload. При отказе не отправлять.
5. Проверку непосредственно перед отправкой дополнительно проверяет Gate агента.
   Повтор event_id возвращает прежнюю проверку; для новой проверки нужен новый intent ID
   со ссылкой на прежний в metadata. checked_at и контекст не бессрочны.
6. Факт отправки — отдельный outbound control с новым event_id и metadata.intent_event_id;
   ответ — inbound control_response с теми же run/session/request. Они оба сохраняются.
7. 201/200 подтверждают COMMIT; только после этого убирать из локальной очереди. Сеть/500/503
   — повтор с теми же ID; 409/422 — очередь ошибок без потери и бесконечных повторов.

Очередь и точки подключения остаются задачей разработчика agent/, здесь его код не менялся.
Формат промпта и структурированного решения: [AGENT_INTERFACE.md](AGENT_INTERFACE.md).
Никакие ветки автоматически не объединяются.

## Импорт JSONL (offline)

Из services/data_api:

```bash
python -m data_api.import_jsonl ../../Logs/2026-10-14/events.jsonl \
  --db ../../state/data-api.sqlite3 --origin historical \
  --run-id historical/2026-10-14 --input-id Logs/2026-10-14/events.jsonl
```

Аналогично telemetry.jsonl/logbook.jsonl. Симуляции: --origin simulator и run-id=simulator/имя;
прототип: legacy и legacy/имя. source=import, metadata.data_origin/input_id/line.
В payload вся запись, raw_text исходная строка; timezone не придумывается. Числовое время
в payload остаётся числом, source_timestamp — строка по общей Schema. Плоская симуляторная
telemetry остаётся полным событием без выдуманных соответствий физическим node_id.

UUID5 из run/session/input_id/номера строки: input_id сохранять при повторе/перемещении файла.
Изменённая строка даст 409-конфликт, история не переписывается. Файл — одна транзакция.
Пустые/невалидные строки становятся import_error с raw_text; --strict откатывает файл на
первой ошибке. --mode envelope сохраняет исходные конверты/ID/run/source, не требует
origin/run-id и откатывает файл на ошибке. UTF-8 BOM снимается как маркер файла.
Импорт читает только указанный файл, агент и оборудование не запускаются.

## Проверки

```bash
cd services/data_api
python -m pytest -q
ruff check .
ruff format --check .
```

Временные SQLite/in-process HTTP: типы JSON, дедупликация и конкурентная доставка,
rollback пакета/импорта, сессии/типы ID, снимки, ошибки и редактирование аудита,
недоступный аудит и команды человека/агента. Подключений к оборудованию нет.

