"""Эмулятор Купола для тестового режима панели (не реальное оборудование).

    python -m integration.demo_dome            # ws://127.0.0.1:8765, ключ DEMO-KEY-123456

Модель купола и правила сервера — из тестов агентной системы (agent/tests/dome_fake). Телеметрия раз в 2 с
с реальным временем кадра. Сценарий: через 20 с Оператор пишет в чат, через 60 с ложный выброс CO₂ на
climate_sensor_01. Сообщения агента появляются в чате эмулятора.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "agent" / "src")]

from tests.dome_fake.server import FakeDome  # noqa: E402
from tests.dome_fake.world import World  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

PORT = int(os.getenv("DEMO_DOME_PORT", "8765"))
KEY = os.getenv("DEMO_DOME_KEY", "DEMO-KEY-123456")


class DemoDome(FakeDome):
    async def __aenter__(self):
        self._server = await serve(self._handle, "127.0.0.1", PORT)
        self.port = PORT
        return self


def start(w: World) -> None:
    for m, name in (("printer_3d_01", "sensor_case.gcode"), ("cnc_01", "pcb_power_board.nc")):
        w.nodes[m].update({"state": "PRINTING" if m == "printer_3d_01" else "RUNNING", "file_name": name, "progress_pct": 10.0})
    w.nodes["printer_3d_01"].update({"real_state": "PRINTING", "webhook_state": "PRINTING"})
    w.job_duration_s = 100_000.0


async def main() -> None:
    world = World()
    start(world)
    dome = DemoDome(key=KEY, period_s=None, telemetry_fn=world.telemetry, on_control=world.control)
    script = [(20, "operator", "Добрый день. Какой статус по принтеру и фрезеру?"), (60, "co2", None)]
    async with dome:
        print(f"Эмулятор Купола: ws://127.0.0.1:{PORT}, ключ {KEY}", flush=True)
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            while script and elapsed >= script[0][0]:
                _, kind, text = script.pop(0)
                if kind == "operator":
                    msg = world.operator_says(text)
                    await dome.broadcast({"type": "meeting_message", "message": msg})
                else:
                    world.nodes["climate_sensor_01"]["co2_ppm"] = 3000.0
                print(f"[{elapsed:.0f} с] сценарий: {kind}", flush=True)
            data = world.telemetry()
            now = time.time()
            for body in data["nodes"].values():
                body["updated_at"] = now
            for m in data["nodes"]["meeting_01"].get("recent_messages") or []:
                m.setdefault("time", now)
            await dome.broadcast({"type": "telemetry", "timestamp": now, "role": "team", "data": data})
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
