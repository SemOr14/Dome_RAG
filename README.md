# Купол — общий проект команды

Общий репозиторий: документация и исторические данные Купола, исследование журналов, агентная система, LLM-слой агента, локальный RAG и отдельный сервис БД/API.

| Каталог | Содержимое | Кто меняет |
|---|---|---|
| `agent/` | Приложение из Kupol.zip: автономный агент, симулятор, панель, тесты, сценарии | Разработчик агентной системы |
| `llm_agent/`, `prompts/`, `data/` | LLM-слой: промпт → DeepSeek → проверенное структурированное решение | Разработчик LLM-слоя |
| `rag/` | Локальный RAG: FAQ, карточки ситуаций, конфликты источников | Разработчик RAG |
| `services/data_api/` | Хранение всех данных, логов и входа/выхода API | SemOr14, ветка БД/API |
| `contracts/` | Единый формат событий, вход/выход API и правила интеграции | Согласованно обе стороны |
| `knowledge/`, `01_API.md` и документы 02–04 | Материалы организаторов | Общая база |
| `Logs/2026-10-14/` | Исходные исторические журналы | Данные сохраняются без изменений |
| `research/logs-2026-10-14/` | Анализ журналов, граф знаний, словарь наблюдавшихся типов | Исследование и БД/API |
| `artifacts/simulator-runs/` | Шесть сохранённых прогонов из архива участника | Архив результатов, не данные реального Купола |
| `docs/` | Обзор импорта, порядок совместной работы, регламент | Общая документация |

## Начало работы

- [Разделение задач и веток](docs/DEVELOPMENT.md)
- [Контракт обмена агента с БД/API](contracts/README.md)
- [Весь вход и выход WebSocket API Купола](contracts/upstream-websocket.md)
- [Сервис БД/API](services/data_api/README.md) и [интерфейс с агентом](services/data_api/AGENT_INTERFACE.md)
- [Разбор наработок участника](docs/imports/kupol-2026-10-26.md)
- [Запуск приложения и симулятора](agent/README.md)
- [API Купола](01_API.md), [задание и оценка](02_Задание_и_оценка.md), [требования к агенту](03_Требования_к_агенту.md)
- [База знаний](knowledge/00_assistant.md)
- [Анализ исторических журналов](research/logs-2026-10-14/README.md)
- [Работающий локальный RAG: установка, CLI, HTTP API и DeepSeek](rag/README.md)

Разработка идёт параллельно в нескольких ветках. В `main` публикуются общие контракты и согласованные результаты слияния. Ключи, `.env`, окружения Python, локальные базы и новые рабочие логи не коммитятся.

## Локальный RAG

В `rag/` добавлены 32 FAQ, 24 карточки ситуаций и 12 конфликтов источников. Поиск объединяет
BM25 и локальные ONNX-эмбеддинги; DeepSeek подключается отдельно. Ответ сохраняет цепочку
«наблюдение → интерпретация → решение → верификация → корректировка» и ссылки на исходники.
RAG не исполняет команды оборудованию. Начало работы: [rag/README.md](rag/README.md).

Материалы, построенные по журналам, описывают только то, что было записано. Наблюдавшиеся значения и связи не следует считать полными схемами, нормативами или подтверждением физической причинности.

## LLM-слой агента

Собирает промпт из данных купола, отправляет его в DeepSeek (единственная разрешённая LLM) и
возвращает структурированное решение. Предложенные моделью действия проверяет код, а не модель.
Команды в Купол LLM-слой не отправляет: это делает агентная система после своего Gate.

```
pip install -r requirements.txt
python run_example.py --dry-run                     # показать собранный промпт, без вызова API
python run_example.py --scenario co2_false_alarm    # также bed_occupied, evening_low_soc
python run_example.py --source data-api --dry-run   # данные запуска из сервиса БД/API
```

Ключ берётся из `DEEPSEEK_API_KEY`, а если переменная не задана — из строки `deepseek:` файла `KEYS_FILE`
(по умолчанию `Keys/team_1.txt`, каталог в `.gitignore`). Остальные настройки описаны в [.env.example](.env.example).

| Что | Где | Как менять |
|---|---|---|
| Шаблон промпта | [prompts/system.md.j2](prompts/system.md.j2), [prompts/user.md.j2](prompts/user.md.j2) | Jinja2: текст и секции правятся без изменения кода (каталог задаёт `PROMPT_TEMPLATE_DIR`) |
| Жёсткие правила и права | [data/hard_rules.yaml](data/hard_rules.yaml) | `text` подставляется в промпт, а `check` включает машинную проверку из [llm_agent/validate.py](llm_agent/validate.py) |
| Граф влияний узлов | [data/influence_graph.yaml](data/influence_graph.yaml) | В промпт попадает подграф вокруг узлов с аномалиями (глубину задаёт `PROMPT_GRAPH_DEPTH`) |
| Телеметрия, каталог, журнал решений | Сервис БД/API ([llm_agent/storage/data_api.py](llm_agent/storage/data_api.py)) или файлы `data/samples/` | Задан `DATA_API_URL` — данные запуска `DATA_API_RUN_ID` из БД, иначе файловые заглушки. Каталог — последний `describe_nodes_response`, запасной — [data/catalog_static.json](data/catalog_static.json) |
| RAG (примеры ситуаций, FAQ) | [rag/](rag/README.md) | Агент берёт согласованные FAQ и карточки ситуаций RAG Купола вместе с конфликтами источников ([llm_agent/rag/dome.py](llm_agent/rag/dome.py)). Без ONNX-модели (`rag/setup.ps1`) поиск идёт только по BM25. Если индекса нет, используется запасной BM25 по `data/rag/`. Выбор задаёт `RAG_BACKEND` |

### Работа с сервисом БД/API

Формат обмена — [AGENT_INTERFACE.md](services/data_api/AGENT_INTERFACE.md). При подключённой БД LLM-слой:

- читает `GET /v1/telemetry/latest` (время и `game_time` — из исходного кадра, `observed_at` попадает в промпт),
  каталог из `describe_nodes_response` и журнал из событий `decision`, `control`, `control_response`,
  `verification`, `meeting_message` того же запуска;
- пишет каждый вызов модели парой `llm_request`/`llm_response` с общим `request_id`, включая невалидный JSON
  (`error: parse_error…`) и сбои сети;
- пишет решение событием `decision` (`decision_id`, `observation_event_ids`, `proposed_controls`, `verification_plan`);
- для каждого предложенного действия вызывает `POST /v1/controls/validate` (`control_intent` со ссылкой на решение).
  Отказ сервера отклоняет действие с причиной `БД/API: …`. `server_check.request_id` агентная система использует
  при отправке команды. Проверка выполняется, только если известна WS-сессия (`DATA_API_SESSION_ID` или
  сессия последнего `auth_response`);
- при недоступной БД складывает события в `state/data_api_outbox.jsonl` и досылает их с прежними `event_id`.
  События с ответом 409/422 сохраняются в `state/data_api_rejected.jsonl`.

Агентная система (`agent/`) при заданном `DATA_API_URL` пишет в ту же БД каждый кадр WebSocket в обе стороны
([agent/src/kupol/dome/datalog.py](agent/src/kupol/dome/datalog.py)): `auth` (ключ скрыт), `describe_nodes`,
телеметрию с `observed_at`, реально отправленные `control` и ответы Купола `control_response`, а также события связи
и таймауты. Её лог дублируется событиями `agent_decision`, `agent_action` (с `request_id` команды) и
`agent_verification`, и журнал промпта LLM-слоя их видит. Чтобы LLM-слой читал тот же запуск, задайте обоим
одинаковый `DATA_API_RUN_ID`.

Проверка без живого Купола и LLM: `python -m pytest tests` (нужны зависимости `services/data_api/requirements.txt`).

Использование в коде:

```python
from llm_agent import DomeLLMAgent, Settings
agent = DomeLLMAgent.from_env(Settings.from_env())   # БД/API, если задан DATA_API_URL, иначе файлы data/
resp = agent.decide("IL_CO2 сработал, станки на удержании")
resp.decision            # AgentDecision: situation, cause, severity, confidence, false_alarm, evidence,
                         #   actions, operator_message, wait_and_watch, rag_refs, risks
resp.accepted_actions    # действия, прошедшие проверку прав, жёстких правил и сервера БД
resp.rejected_actions    # отклонённые действия с причинами
resp.verdicts[i].server_check   # status, reasons, request_id, intent_event_id
resp.decision_event_id   # событие decision в БД
resp.llm_call            # model, tokens, latency_ms, ok, error
```

Если LLM недоступна или дважды вернула невалидный JSON, ответ приходит с `ok=False` и без действий:
агент продолжает работу по правилам.

## Секреты

Не добавляйте в Git ключи команд, пароли, токены, `.env` и журналы с секретами. Ключи, ранее находившиеся в истории этого репозитория, следует заменить; удаление файла в новом коммите не удаляет его из прошлых коммитов.
