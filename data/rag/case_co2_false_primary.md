---
id: case-co2-false-primary
title: "Ложный IL_CO2: основной датчик врёт, два других в норме"
tags: [IL_CO2, climate_sensor_01, set_data_trust, set_primary_sensor, ложная тревога]
---
Признаки: IL_CO2 TRIPPED, принтер и фрезер на удержании (hold_reason=IL_CO2). Основной датчик
climate_sensor_01 показывает ~3000 ppm, climate_sensor_02 и climate_sensor_03 — 770–790 ppm. Людям не душно.

Что было (журнал 14.10): основной датчик показал 3041 ppm, остальные 773 и 787. Пометили датчик ненадёжным,
переключили автоматику на climate_sensor_02, станки продолжили.

Порядок действий:
1. dome_automation_01.set_data_trust {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"}.
2. dome_automation_01.set_primary_sensor "climate_sensor_02".
3. dome_automation_01.reset_interlock "IL_CO2" — проверить, что interlocks.IL_CO2.state = ARMED.
4. Только после этого resume принтера и фрезера; проверить state = PRINTING / RUNNING.

Осторожно: у climate_sensor_02/03 есть reading_delay_s (15 и 30 минут) — их значение отстаёт. Настоящий рост
CO2 виден на всех трёх датчиках сразу (см. case-co2-real-supply-fault). Оператора не беспокоить.
