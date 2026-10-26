import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BASE = 'http://62.118.24.234:48765'
OUT = Path(__file__).parent / 'источники'
OUT.mkdir(parents=True, exist_ok=True)

def ask(message):
    req = Request(BASE + '/api/chat', data=json.dumps({'message': message}, ensure_ascii=False).encode('utf-8'), headers={'Content-Type': 'application/json'})
    started = time.monotonic()
    with urlopen(req, timeout=100) as response:
        result = json.load(response)
    record = {'time': datetime.now(timezone.utc).isoformat(), 'elapsed_seconds': round(time.monotonic()-started, 2), 'question': message, 'response': result}
    with (OUT / 'диалог_справка.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)

if __name__ == '__main__':
    for message in json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')):
        try:
            ask(message)
        except Exception as exc:
            print(type(exc).__name__, str(exc), flush=True)
