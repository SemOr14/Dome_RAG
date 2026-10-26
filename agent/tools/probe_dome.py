"""Проверка связи с куполом ТОЛЬКО НА ЧТЕНИЕ: auth → describe_nodes → несколько снимков телеметрии.

Ничего не отправляет в ``control``. Ключи не печатаются. Опционально сохраняет каталог узлов и
один снимок (без ключей) в каталог фикстур::

    python tools/probe_dome.py --snapshots 3 --save tests/fixtures
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kupol.dome.env import load_settings  # noqa: E402


async def probe(snapshots: int, save: Path | None) -> int:
    st = load_settings()
    if not st.dome_key:
        print("Нет ключа купола (DOME_KEY или файл ключей)")
        return 2
    print(f"Подключение к {st.dome_url} (ключ: {st.sources.get('DOME_KEY', 'нет')})")
    async with websockets.connect(st.dome_url, max_size=50_000_000, open_timeout=10) as ws:
        await ws.send(json.dumps({"type": "auth", "key": st.dome_key}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
        print("auth:", {k: v for k, v in auth.items() if k != "key"})
        if not auth.get("success"):
            return 1
        await ws.send(json.dumps({"type": "describe_nodes", "request_id": "probe-1"}))
        catalog: dict[str, Any] | None = None
        shots: list[dict[str, Any]] = []
        extra: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while (catalog is None or len(shots) < snapshots) and loop.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
            kind = msg.get("type")
            if kind == "describe_nodes_response":
                catalog = msg
            elif kind == "telemetry":
                shots.append(msg)
            elif kind in ("meeting_message", "session_revoked"):
                extra.append(msg)
        if catalog:
            nodes = catalog.get("nodes", [])
            print(f"describe_nodes: success={catalog.get('success')}, узлов {len(nodes)}")
            for n in nodes:
                print(f"  {n['node_id']:26} controls={len(n.get('controls', []))} operator={n.get('operator_controls', [])}")
        for i, s in enumerate(shots):
            data = s.get("data", {})
            print(f"телеметрия #{i + 1}: role={s.get('role')} узлов {len(data.get('nodes', {}))} game_time={data.get('game_time')}")
        print(f"сообщений чата/служебных за время пробы: {len(extra)}")
        if save and catalog and shots:
            save.mkdir(parents=True, exist_ok=True)
            (save / "dome_catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
            (save / "dome_telemetry.json").write_text(json.dumps(shots[-1], ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"сохранено в {save}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshots", type=int, default=2)
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()
    return asyncio.run(probe(args.snapshots, args.save))


if __name__ == "__main__":
    raise SystemExit(main())
