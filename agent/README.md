# Штаб «Купол»

> Общий репозиторий: запускайте команды из `agent/`. База знаний, `Logs/` и документы API лежат на уровень выше; задайте `DOME_RAG_DIR=..` или скопируйте обновлённый `.env.example` в `.env`. Записи прогонов из архива — в `../artifacts/simulator-runs/`, регламент — [PDF](../docs/reference/competition-regulations.pdf). Разработка агента идёт в `work/agent-system`, граница БД/API — [контракт](../contracts/README.md).


Система Штаба для соревнования «Лига автономных систем — Купол»: адаптер (симулятор или реальный Комплекс), ядро
(состояние, журнал, предохранитель команд, детекторы, сверка источников, вердикты, энергетика, пробы), три агента на
DeepSeek V4 Flash с резервными сценариями, панель Штаба и CLI. Задание — `TZ_kupol.md` и `TZ_kupol_addendum.md`,
регламент — `Регламент_Лига_автономных_систем_Купол.pdf`.

Главный принцип: **код считает, модель объясняет.** Пороги, уверенность, уровень реакции и допуск команд решает код;
LLM формулирует гипотезы и тексты рекомендаций. Любая команда уходит в Комплекс только через `core/guard.py`.

## Запуск за 5 минут

Нужен Python 3.11+. Ключ API не нужен: по умолчанию LLM работает в режиме `mock` (офлайн-заглушки).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"

# Прогон сценария на симуляторе до конца игры (без ключа, без сети)
.venv\Scripts\python -m kupol simulate --scenario scenarios/basic.yaml --seed 42 --speed 0

# Отчёт по прогону и пошаговое воспроизведение
.venv\Scripts\python -m kupol report logs\<прогон>
.venv\Scripts\python -m kupol replay logs\<прогон>

# Панель Штаба в браузере (симулятор в реальном времени)
.venv\Scripts\python -m kupol dashboard --scenario scenarios/basic.yaml

# Тесты, покрытие ядра, типы
.venv\Scripts\python -m pytest --cov=src/kupol/core --cov-fail-under=80
# по умолчанию полные игры на симуляторе (метка slow) пропускаются; перед релизом и после правок детекторов/вердиктов:
.venv\Scripts\python -m pytest -m "slow or not slow"
.venv\Scripts\python -m mypy
```

На Linux/macOS замените `.venv\Scripts\python` на `.venv/bin/python`.

## Соревновательный режим (Dome)

Агент для реального Купола (WebSocket API организаторов) запускается одной командой и по умолчанию работает
**только на чтение** — управление включается явно (`DOME_ALLOW_CONTROL=1`):

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py --dry-run          # наблюдение и решения без команд
.venv\Scripts\python tools\check_agent_log.py logs\agent_<время>.jsonl
```

Настройки — `.env` / окружение / файл ключей, политика команд и пороги — `dome.yaml`. Подробно:
[docs/DOME_RUNBOOK.md](docs/DOME_RUNBOOK.md), устройство: раздел «Соревновательный рантайм» в
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), решения 30–41 в [docs/DECISIONS.md](docs/DECISIONS.md).

## Команды CLI

| Команда | Что делает |
|---|---|
| `simulate --scenario S [--seed N --speed K --duration T --auto-approve --sim-operator --llm-fail timeout\|bad_json]` | Прогон сценария на симуляторе. `--speed 0` — без пауз. `--auto-approve` подтверждает команды `needs_human` (только для headless-прогонов; в журнале актёр `sim-auto-approve`). `--sim-operator` — симулированный Оператор отвечает на вопросы проб. |
| `run [--config C]` | Игра на реальном Комплексе через `adapter/real.py` (утром, после адаптации). |
| `replay RUN_DIR` | Воспроизвести сохранённый прогон по журналу. |
| `report RUN_DIR [--out F]` | Итоговый отчёт в Markdown: хронология, тревоги, команды, вердикты, метрики качества. |
| `benchmark --scenarios scenarios/ --seeds 1-20` | Все сценарии на нескольких seed, сводка метрик → `docs/BENCHMARK.md`. |
| `dashboard [--scenario S]` | Панель Штаба (Streamlit). Без `--scenario` — реальный адаптер. |

Общие параметры: `--config` (по умолчанию `config.yaml`), `--logs-dir`, `--llm-mode mock|dev|competition`.

Режимы LLM: `mock` — заглушки без сети; `dev` — реальная модель, ключ в `DEEPSEEK_API_KEY` (см. `.env.example`);
`competition` — реальная модель и запрет любой модели, кроме DeepSeek V4 Flash (регламент п. 10.6).

## Сценарии

`scenarios/`: `quiet` (штатная игра, тревог быть не должно), `basic` (ложный CO2, дрейф, отказ исполнителя, потеря
связи), `cascade_heavy` (энергетический каскад), `hidden` (скрытые события: дрейф, нагрузка вне телеметрии, тихий
брак, залипание, потеря шума, сбой меток времени, запоздавший исполнитель), `traps` (ловушки: ложный сигнал, затем
реальный; каскадное действие), `mixed` (всё вперемешку). Симулятор пишет эталонную разметку `ground_truth.jsonl`;
агенты и детекторы её не видят, её читают только метрики и отчёт.

## Что внутри

```
config.yaml                 все пороги, мощности, белый список команд, веса свидетельств, проб, обхода
src/kupol/
  adapter/                  base (интерфейс), simulator (+ scenario), real (заготовка под утро)
  core/                     models, config, state, journal, guard, detectors, baseline, crosscheck,
                            verdicts, response, probes, round, energy, metrics
  agents/                   llm, monitor, diagnost, energy_planner, symptoms, prompts/
  engine.py                 главный цикл (используют CLI и панель)
  ui/dashboard.py           панель Штаба на Streamlit
  ui/charts.py              вкладка «Графики»: величины и диагностика решений (Altair)
  dome/                     соревновательный рантайм: клиент WebSocket, Gate, плейбуки, чат Оператора, RAG, лог агента
main.py                     запуск агента для Dome одной командой
  report.py, benchmark.py   отчёты и бенчмарк
scenarios/  tests/  docs/
```

## Документация

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — схема модулей, потоки данных, жизненный цикл вердикта, уровни реакции.
- [docs/MORNING_CHECKLIST.md](docs/MORNING_CHECKLIST.md) — пошаговая адаптация под реальный Купол.
- [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) — все выдуманные допущения.
- [docs/DECISIONS.md](docs/DECISIONS.md) — принятые решения и отступления от ТЗ.
- [docs/SYMPTOM_MATRIX.md](docs/SYMPTOM_MATRIX.md) — матрица «симптом → причины → проверка».
- [docs/BENCHMARK.md](docs/BENCHMARK.md) — результаты бенчмарка.
