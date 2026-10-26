# Dome RAG — материалы и анализ Купола

Репозиторий содержит документацию API Купола, базу знаний и анализ локальных журналов за 14 октября 2026 года.

## С чего начать

- [API Купола](01_API.md)
- [Задание и оценка](02_Задание_и_оценка.md)
- [Требования к агенту](03_Требования_к_агенту.md)
- [База знаний](knowledge/00_assistant.md)
- [Исследование журналов](research/logs-2026-10-14/README.md)
- [Правила совместной работы](CONTRIBUTING.md)
- [Работающий локальный RAG: установка, CLI, HTTP API и DeepSeek](rag/README.md)

## Локальный RAG

В `rag/` добавлены 32 FAQ, 24 карточки ситуаций и 12 конфликтов источников. Поиск объединяет
BM25 и локальные ONNX-эмбеддинги; DeepSeek подключается отдельно. Ответ сохраняет цепочку
«наблюдение → интерпретация → решение → верификация → корректировка» и ссылки на исходники.
RAG не исполняет команды оборудованию. Начало работы: [rag/README.md](rag/README.md).

Материалы, построенные по журналам, описывают только то, что было записано. Наблюдавшиеся значения и связи не следует считать полными схемами, нормативами или подтверждением физической причинности.

## Агент: LLM-слой

Собирает промпт из данных купола, отправляет его в DeepSeek (единственная разрешённая LLM) и
возвращает структурированное решение. Предложенные моделью действия проверяет код, а не модель.

```
pip install -r requirements.txt
python run_example.py --dry-run                     # показать собранный промпт, без вызова API
python run_example.py --scenario co2_false_alarm    # также bed_occupied, evening_low_soc
```

Ключ берётся из `DEEPSEEK_API_KEY`, а если переменная не задана — из строки `deepseek:` файла `KEYS_FILE`
(по умолчанию `Keys/team_1.txt`, каталог в `.gitignore`). Остальные настройки описаны в [.env.example](.env.example).

| Что | Где | Как менять |
|---|---|---|
| Шаблон промпта | [prompts/system.md.j2](prompts/system.md.j2), [prompts/user.md.j2](prompts/user.md.j2) | Jinja2: текст и секции правятся без изменения кода (каталог задаёт `PROMPT_TEMPLATE_DIR`) |
| Жёсткие правила и права | [data/hard_rules.yaml](data/hard_rules.yaml) | `text` подставляется в промпт, а `check` включает машинную проверку из [agent/validate.py](agent/validate.py) |
| Граф влияний узлов | [data/influence_graph.yaml](data/influence_graph.yaml) | В промпт попадает подграф вокруг узлов с аномалиями (глубину задаёт `PROMPT_GRAPH_DEPTH`) |
| Каталог узлов | [data/catalog_static.json](data/catalog_static.json) | Запасной вариант на случай, если нет `describe_nodes` |
| Телеметрия, журнал решений | `data/samples/` | Пока файловые заглушки ([agent/storage/file_store.py](agent/storage/file_store.py)). Для БД нужно реализовать протоколы из [agent/storage/base.py](agent/storage/base.py) |
| RAG (примеры ситуаций, FAQ) | `data/rag/*.md`, `data/rag/*.jsonl` | Сейчас работает заглушка с BM25 ([agent/rag/keyword.py](agent/rag/keyword.py)). Для векторного поиска нужно реализовать `Retriever.search` из [agent/rag/base.py](agent/rag/base.py) |

Использование в коде:

```python
from agent import DomeLLMAgent, Settings
agent = DomeLLMAgent(Settings.from_env(), telemetry=..., journal=..., catalog_repo=..., retriever=...)
resp = agent.decide("IL_CO2 сработал, станки на удержании")
resp.decision            # AgentDecision: situation, cause, severity, confidence, false_alarm, evidence,
                         #   actions, operator_message, wait_and_watch, rag_refs, risks
resp.accepted_actions    # действия, прошедшие проверку прав и жёстких правил
resp.rejected_actions    # отклонённые действия с причинами
resp.llm_call            # model, tokens, latency_ms, ok, error (для записи llm_call в лог)
```

Если LLM недоступна или дважды вернула невалидный JSON, ответ приходит с `ok=False` и без действий:
агент продолжает работу по правилам.

## Секреты

Не добавляйте в Git ключи команд, пароли, токены, `.env` и журналы с секретами. Ключи, ранее находившиеся в истории этого репозитория, следует заменить; удаление файла в новом коммите не удаляет его из прошлых коммитов.
