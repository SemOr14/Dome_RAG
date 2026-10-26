---
id: case-printer-bed-webhook
title: "Деталь на столе и ложный webhook COMPLETED"
tags: [printer_3d_01, bed_clear, BED_COLLISION, webhook_state, thermal_insulation_01, set_lock]
---
Признаки: printer_3d_01.webhook_state = COMPLETED, но state = PRINTING, progress_pct < 100, z_mm растёт.

Что было (журнал 14.10): агент поверил webhook_state=COMPLETED и открыл замок бокса, а принтер печатал (33 %).
Дверь, к счастью, не открыли. Вывод: при длинной печати webhook врёт — смотреть state, progress_pct и z_mm.

Правильный порядок после завершения печати:
1. Подтвердить завершение: state = COMPLETED, progress_pct = 100.
2. thermal_insulation_01.set_lock {"lock": 0, "locked": false} и {"lock": 1, "locked": false}.
3. Короткое сообщение Оператору: «Печать <файл> завершена, бокс ФабЛаб открыт — заберите деталь».
4. Ждать bed_clear = true. Только потом start_job следующего файла. Запуск при bed_clear=false →
   BED_COLLISION и ALARM на 20 минут.
5. После снятия детали — снова закрыть замки.
