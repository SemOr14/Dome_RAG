"""Read public Kupol telemetry directly, without the website or chatbot."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen

URL = "http://62.118.24.234:48765/api/telemetry"


def get_telemetry():
    with urlopen(URL, timeout=10) as response:
        data = json.load(response)
    data["receivedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return data


def print_summary(data):
    lines = data.get("lines", [])
    names = data.get("lineNames", [])
    states = data.get("lineStates", [])
    battery = data.get("battery", {})
    diesel = data.get("diesel", {})
    game_time = data.get("gameTime", {})
    print(f"Получено: {data['receivedAtUtc']}; время купола: сутки {game_time.get('day')}, час {game_time.get('hour')}")
    print(f"Солнце: {data.get('solarKw')} кВт; ветер: {data.get('windKw')} кВт; АКБ: {battery.get('soc')} %")
    print(f"Дизель: {'включён' if diesel.get('on') else 'выключен'}; топливо: {diesel.get('fuel')} %")
    print(f"Климат: {data.get('temp')} °C, {data.get('hum')} %, CO₂ {data.get('co2')} ppm")
    for i, load in enumerate(lines):
        name = names[i] if i < len(names) else f"Линия {i + 1}"
        state = states[i] if i < len(states) else "?"
        print(f"  {i + 1}. {name}: {load:.3f} кВт ({state})")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="show complete response as JSON")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="repeat at this interval")
    args = parser.parse_args()
    if args.watch is not None and args.watch <= 0:
        parser.error("--watch must be positive")

    try:
        while True:
            data = get_telemetry()
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print_summary(data)
            if args.watch is None:
                break
            print()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
