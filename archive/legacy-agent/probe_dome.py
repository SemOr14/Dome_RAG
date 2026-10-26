"""Быстрая проверка адреса купола: python probe_dome.py [ws://host:port]

Показывает роль, каталог прав и первый срез телеметрии. Ничего не меняет.
"""
from __future__ import annotations

import asyncio
import json
import sys

from config import Config
from dome import DomeClient
from agent_log import AgentLog


async def main(url: str | None) -> int:
    cfg = Config(dome_url=url) if url else Config()
    log = AgentLog(cfg.log_dir, run_id="probe")
    dome = DomeClient(cfg, log)
    task = asyncio.create_task(dome.run())
    try:
        await asyncio.wait_for(dome.connected.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ за 15 с:", cfg.dome_url)
        await dome.stop()
        task.cancel()
        return 1
    print(f"подключено: {cfg.dome_url}")
    print(f"роль: {dome.role}, команда: {dome.team}")
    catalog = await dome.describe()
    print(f"узлов в каталоге: {len(catalog.get('nodes') or [])}")
    for node in (catalog.get("nodes") or [])[:100]:
        actions = node.get("controls") or []
        print(f"  {node.get('node_id'):28s} {len(actions):2d} действий: {', '.join(actions[:8])}")
    for _ in range(10):
        if dome.data.get("nodes"):
            nodes = dome.data["nodes"]
            print(f"\nтелеметрия: {len(nodes)} узлов, game_time={dome.data.get('game_time')}")
            for nid in list(nodes)[:100]:
                print(f"  {nid:28s} {json.dumps(nodes[nid], ensure_ascii=False)[:110]}")
            break
        await asyncio.sleep(1)
    await dome.stop()
    task.cancel()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
