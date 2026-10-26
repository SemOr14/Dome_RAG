# Управление производством — локальный контракт v1

База: `7292ae75eecac56cb9f4bca12f335504559a2af3`. Новые feature-ветки основаны на одном
контрактном коммите. Межкомпонентные изменения для интеграции: Runner/Gate/Client проверяют
`ControlPolicy`; Hub владеет серверным режимом; UI обращается только к Hub; события сохраняются
через существующий envelope/Store. RAG и исходные записи не изменяются.

Профиль задаётся сервером: `DEPLOYMENT_PROFILE=competition|service`. Объём API:
`DOME_API_SCOPE=history|full` (до уточнения владельца — history). История не подтверждает текущее
состояние, не предоставляет upload/start/control и отправку в meeting_01.

AgentMode: off/recommendation/autonomous; algorithms_enabled независим. Каждая смена режима
увеличивает policy_epoch, отменяет несовместимые несостоявшиеся отправки и хвост жёлтого плана.
`confirmed=true` не отменяет Gate, профиль, capabilities, свежесть и ревизию политики.

OperatingState: normal/manual_after_yellow/shutting_down. manual_after_yellow — отметка события,
не блокировка. shutting_down только в памяти завершающегося процесса. Перезапуск не требует reset.

Команда: operation_id, causal_event_id, actor (operator/algorithm/agent/safety), policy_epoch,
agent_mode, run_id/session_id, sent/accepted отдельно от verified. Совместимость: автор нового
типа хранится в metadata.actor, существующие source остаются прежними. Таймаут после отправки
— ambiguous, сверка текущего задания, без автоматического повтора. Exactly-once не обещается.

EquipmentCapabilities, ProductionJob, Incident определены в kupol.dome.control. Неизвестная
capability выключена. Источник ETA и время измерения обязательны; один job — один файл, без очереди.
Инцидент: open → acknowledged → resolved; ack не разрешает resume и не сбрасывает интерлок.
Проекции восстанавливаются из новых event_type существующего журнала, миграция исходных записей
не нужна. Секреты редактируются существующим Redactor; REST наследует авторизацию и HTTP-аудит.

Примеры переходов:

```json
{"agent_mode":"off","algorithms_enabled":true,"policy_epoch":1}
{"agent_mode":"off","algorithms_enabled":false,"policy_epoch":2}
{"operating_state":"manual_after_yellow","agent_mode":"off","algorithms_enabled":false,"policy_epoch":3}
{"agent_mode":"autonomous","algorithms_enabled":true,"operating_state":"normal","policy_epoch":4}
```

Красная увеличивает ревизию и завершает все процессы приложения. Следующий обычный запуск
использует обычные стартовые настройки. Старые неподтверждённые действия сверяются без переотправки.
Линия 7, критические узлы и необходимые переходные охлаждение/вытяжка сохраняются при жёлтой;
линия 1 смешанная. Подробная политика отключения реализуется отдельной feature-веткой.
