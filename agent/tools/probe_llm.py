"""Проверка DeepSeek одним коротким запросом (ключ берётся из окружения/.env/файла ключей, не печатается).

    python tools/probe_llm.py "Что такое IL_CO2?"
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kupol.dome.agentlog import AgentLog  # noqa: E402
from kupol.dome.config import load_dome_config  # noqa: E402
from kupol.dome.env import load_settings  # noqa: E402
from kupol.dome.llm import DeepSeek, LlmAdvisor  # noqa: E402
from kupol.dome.rag import build_rag  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    question = sys.argv[1] if len(sys.argv) > 1 else "Что делать, если основной датчик CO2 показывает 3000 ppm, а два других 780?"
    st = load_settings()
    cfg = load_dome_config("dome.yaml")
    if not st.deepseek_key:
        print("Нет ключа DeepSeek")
        return 2
    log = AgentLog(tempfile.mkdtemp(prefix="probe_llm_"), secrets=[st.deepseek_key, st.dome_key])
    llm = DeepSeek(st.deepseek_key, st.deepseek_base_url, st.deepseek_model or cfg.advisor.model, log, cfg.advisor)
    rag = build_rag(st.rag_dir) if Path(st.rag_dir).is_dir() else None
    print(f"модель {llm.model}, фрагментов в базе знаний: {len(rag) if rag else 0}")
    answer = LlmAdvisor(rag, llm).answer(question, {"CO2_ppm": {"climate_sensor_01": 3000, "climate_sensor_02": 780, "climate_sensor_03": 790}})
    print("ответ:", answer)
    print("лог:", log.path)
    return 0 if answer else 1


if __name__ == "__main__":
    raise SystemExit(main())
