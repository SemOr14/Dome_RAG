"""Прогноз важных параметров и подозрительные наблюдения «на будущее»: расчёт, промпт, БД, лимит один за решение."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("jinja2")
pytest.importorskip("openai")

from kupol.dome.forecast import Forecaster, forecast_lines  # noqa: E402
from kupol.dome.suspicions import SuspicionStore  # noqa: E402
from llm_agent.schemas import AgentDecision  # noqa: E402
from llm_agent.llm_client import LLMResult  # noqa: E402
from tests.dome_fake.world import World  # noqa: E402
from tests.test_dome_llm_brain import QUIET, FakeLLM, LlmHarness  # noqa: E402
from tests.test_dome_scenarios import run, start_work  # noqa: E402


def feed(fc: Forecaster, soc_from: float, per_min: float, minutes: int = 20, power_w: float | None = None) -> dict:
    nodes: dict = {}
    for m in range(minutes):
        nodes = {"solar_inverter_01": {"battery_soc_pct": soc_from + per_min * m, "updated_at": m, "battery_capacity_kwh": 2.5,
                                       **({"battery_power_w": power_w} if power_w is not None else {})}}
        fc.observe(nodes, 1000.0 + 60.0 * m)
    return nodes


def test_battery_discharge_gives_time_to_critical_and_to_zero() -> None:
    fc = Forecaster()
    nodes = feed(fc, 80.0, -0.5)  # -30 %/ч
    soc = next(f for f in fc.compute(nodes) if f["id"] == "battery_soc")
    assert soc["trend"] == "падает" and soc["rate_per_h"] == pytest.approx(-30.0, abs=0.5)
    reach = {r["threshold"]: r["eta_h"] for r in soc["reaches"]}
    assert reach[25.0] == pytest.approx((70.5 - 25.0) / 30.0, abs=0.05)  # SOC в последней точке 70.5 %
    assert reach[0.0] > reach[25.0]
    assert "Заряд АКБ" in forecast_lines([soc])[0] and "критический заряд" in forecast_lines([soc])[0]


def test_battery_estimate_by_discharge_power() -> None:
    fc = Forecaster()
    nodes = feed(fc, 60.0, 0.0, power_w=-500.0)  # заряд не меняется, но идёт разряд 500 Вт
    soc = next(f for f in fc.compute(nodes) if f["id"] == "battery_soc")
    by_power = soc["by_power"]
    assert by_power["discharge_w"] == 500.0
    assert by_power["reaches"][0]["eta_h"] == pytest.approx((60.0 - 25.0) / 100.0 * 2500.0 / 500.0, abs=0.01)


def test_noise_is_not_a_trend_and_charging_is_not_a_discharge_warning() -> None:
    fc = Forecaster()
    nodes = {}
    for m, v in enumerate([50, 50.4, 49.7, 50.3, 49.8, 50.2, 49.9, 50.1, 50.0, 49.7]):
        nodes = {"solar_inverter_01": {"battery_soc_pct": v, "updated_at": m}}
        fc.observe(nodes, 1000.0 + 60.0 * m)
    soc = next(f for f in fc.compute(nodes) if f["id"] == "battery_soc")
    assert "reaches" not in soc and soc["trend"].startswith("стабильно")
    fc = Forecaster()
    nodes = feed(fc, 30.0, 0.5)  # заряд растёт
    soc = next(f for f in fc.compute(nodes) if f["id"] == "battery_soc")
    assert soc["trend"] == "растёт" and [r["threshold"] for r in soc["reaches"]] == [100.0]


def test_repeated_frames_do_not_train_the_trend() -> None:
    fc = Forecaster()
    for k in range(30):  # один и тот же кадр приходит много раз
        fc.observe({"solar_inverter_01": {"battery_soc_pct": 50.0, "updated_at": 7}}, 1000.0 + 2.0 * k)
    assert len(fc._series[("solar_inverter_01", "battery_soc_pct")]) == 1


def test_decision_takes_one_suspicion_and_survives_garbage() -> None:
    first = {"text": "CO2 у окна растёт без людей", "node_id": "climate_sensor_02", "param": "co2_ppm", "check": "сравнить через час"}
    base = {"situation": "s"}
    assert AgentDecision.model_validate({**base, "suspicion": first}).suspicion.node_id == "climate_sensor_02"
    assert AgentDecision.model_validate({**base, "suspicion": [first, {"text": "второе"}]}).suspicion.text == first["text"]  # максимум одно
    assert AgentDecision.model_validate({**base, "suspicion": "строкой"}).suspicion.text == "строкой"
    for junk in (None, {}, [], "", {"text": " "}, 5):
        assert AgentDecision.model_validate({**base, "suspicion": junk}).suspicion is None


def test_store_dedups_expires_and_survives_restart(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "s.jsonl"
    store = SuspicionStore(path, lambda: now[0])
    assert store.add({"text": "Странный дрейф", "node_id": "n1"}) is not None
    assert store.add({"text": "странный  дрейф!", "node_id": "n1"}) is None  # дубль
    assert store.add({"text": "Странный дрейф", "node_id": "n2"}) is not None  # другой узел
    assert len(SuspicionStore(path, lambda: now[0]).open()) == 2  # после перезапуска записи на месте
    now[0] += 25 * 3600
    assert store.open() == []  # устарели


def test_prompt_has_forecast_db_events_and_suspicion_comes_back(tmp_path: Path) -> None:
    prompts: list[str] = []

    class Spy(FakeLLM):
        def chat(self, messages, **kwargs):
            prompts.append(messages[1]["content"])
            assert "Иерархия узлов" in messages[0]["content"] and "line:3 → printer_3d_01" in messages[0]["content"]
            return super().chat(messages, **kwargs)

    sent = {"n": 0}

    def answer(trigger: str):
        sent["n"] += 1
        return {**QUIET, "suspicion": {"text": "CO2 растёт без людей", "node_id": "climate_sensor_02", "param": "co2_ppm",
                                       "why": "ночью, двери закрыты", "check": "сравнить с climate_sensor_03"}} if sent["n"] <= 2 else QUIET

    async def scenario() -> None:
        w = World()
        start_work(w)
        llm = Spy(answer)
        async with LlmHarness(tmp_path, w, llm) as h:
            await h.step(10)
            h.brain.operator_request("Как дела?")  # второй повод для модели: она должна увидеть свою прошлую запись
            await h.step(10)
            assert len(prompts) >= 2, f"модель вызвана {len(prompts)} раз"
            assert all("# Прогнозы" in p and "# Положение затронутых узлов" in p and "# Сверка с близкими" in p for p in prompts) and any("Заряд АКБ" in p for p in prompts)
            # Подозрение записано один раз (повтор того же — дубль), в лог и в файл на диск
            written = [r for r in h.records("observation") if r.get("suspicion")]
            assert len(written) == 1 and written[0]["suspicion"]["node_id"] == "climate_sensor_02"
            stored = [json.loads(x) for x in (tmp_path / "state" / "suspicions.jsonl").read_text(encoding="utf-8").splitlines()]
            assert len(stored) == 1
            # Следующий вызов модели видит запись в своём разделе
            later = [p for p in prompts if "CO2 растёт без людей" in p]
            assert later and "Твои подозрительные наблюдения" in later[-1] and "сравнить с climate_sensor_03" in later[-1]

    run(scenario())
