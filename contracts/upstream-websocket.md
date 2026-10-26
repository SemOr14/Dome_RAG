# Весь вход/выход WebSocket API Купола

Источник: общий `01_API.md` (разделы 2–6 и 8). Это внешний API организаторов. Направления указаны относительно агента. Каждое сообщение передаётся отдельным JSON-объектом UTF-8. Неизвестные поля сохраняются в payload.

| Направление | type | Обязательный вход / содержимое выхода |
|---|---|---|
| исходящий | auth | `key:string` (при хранении скрывать) |
| входящий | auth_response | `success:boolean`; успех: `role:team/guest`, `team:integer`; ошибка: `error:string` |
| исходящий | ping | `request_id:string/number` |
| входящий | ping_response | `request_id`, `success:boolean` |
| исходящий | describe_nodes | `request_id` |
| входящий | describe_nodes_response | `request_id`, `success`; успех: `nodes:array` объектов каталога; ошибка: `error` |
| исходящий | control | `request_id,node_id:string,action:string`, необязательное `value:any JSON` |
| входящий | control_response | `request_id,success`; `node_id,action,mode:real/virtual`; при ошибке `error` |
| исходящий | query | `request_id,node_id:string,query:string`, необязательный `params:object` |
| входящий | query_response | `request_id,success,node_id,query`; успех: `result:any JSON,mode`; ошибка: `error` |
| входящий | telemetry | `timestamp:number,role:team/guest,data:object` с `nodes:{node_id:object}` и `game_time:{day:integer,hour:number}` |
| входящий | meeting_message | `message:object` — запись чата, описание ниже |
| входящий | meeting_cleared | Уведомление очистки чата; сохранить все полученные поля |
| входящий | session_revoked | `reason:string`; соединение затем закрывается |

`request_id` отражается сервером в ответе с исходным значением; для корреляции сохраняются run_id и session_id. Ответы могут чередоваться с телеметрией и чатом. Авторизация выполняется первым сообщением; отсутствие auth за 10 секунд или неверный ключ — закрытие 4401. Отзыв сессии — 4403, затем новое соединение и авторизация. Гость получает только meeting_01 и не управляет оборудованием.

## Каталог прав

Элемент `nodes`: `node_id,code_name,title,system,category` — строки; `controls,operator_controls,queries,service_actions` — массивы строк; `control_aliases` — объект `основное имя: [алиасы]`. Актуальный каталог после подключения — источник прав. Физические operator_controls выполняет Оператор.

## Запросы данных

- `printer_3d_01/list_files`: params `{}`; result — массив файлов с name, size_bytes и доступными оценками времени/материала. У реального устройства поля могут отличаться.
- `cnc_01/list_files`: params `{validate:true}` по необходимости; result — массив файлов с name,size_bytes,valid,error.
- `meeting_01/read_messages`: params `{since_id?:integer,limit?:integer}`; limit 1–200, result — сообщения по возрастанию id.

Сообщение чата: `{id:integer,time:number,sender:{role:operator/team/guest,team:integer/null,name:string},to:string/null,text:string}`. Отправка через `control` узла meeting_01 с action post_message: value — строка или `{text,to?,name?}`. text до 4000 символов, to до 64. Ответ содержит `message`.

## Управление и телеметрия

Все действия, алиасы, значения value, поля узлов и ограничения описаны один раз в [разделе 8 API](../01_API.md#8-справочник-узлов). Статический извлечённый каталог — [JSON](../research/logs-2026-10-14/Каталог_узлов_по_API.json). Наблюдавшиеся типы данных — [словарь](../research/logs-2026-10-14/Словарь_данных_по_логам.json). Наблюдавшийся тип не задаёт нормативный диапазон.

`success:true` означает принятие команды. Эффект подтверждается последующей телеметрией и отдельной записью verification. Служебные поля узла online,updated_at,status и поля UNKNOWN/null сохраняются. Устаревшее состояние не считается новым измерением.

## Запись в БД

Для любого входящего/исходящего сообщения создаётся конверт [event.schema.json](event.schema.json). payload содержит исходный JSON; raw_text — исходную строку, если она нужна для аудита/не разбирается. Внутренними событиями записываются потеря соединения, код закрытия, таймаут запроса, ошибка JSON, отказ доставки и проверка результата. Пароли, auth.key, Authorization, API-ключи и токены скрываются до хранения.

Ограничения внешнего сервера: не больше 5 запросов/команд в секунду, чат — 10 за 10 секунд, сообщение до 32 МБ. Эти лимиты не отменяют запись всех уже принятых событий в БД.
