from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
INDEX = ROOT / 'index'


def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if re.fullmatch(r'[A-Z_]+', key.strip()):
                    os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def redact(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    for key in ['DEEPSEEK_API_KEY', 'DOME_KEY']:
        secret = os.getenv(key, '')
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', '[REDACTED]', text)


def log_event(kind, **values):
    directory = Path(os.getenv('RAG_LOG_DIR', str(ROOT / 'logs')))
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('rag_' + dt.datetime.now().strftime('%Y%m%d') + '.jsonl')
    entry = dict(ts=dt.datetime.now().astimezone().isoformat(timespec='milliseconds'), kind=kind, **values)
    with path.open('a', encoding='utf-8') as out:
        out.write(redact(entry) + '\n')
        out.flush()


def source_path(reference):
    path = (DATA / reference['path']).resolve()
    if not path.is_relative_to((DATA / 'sources').resolve()):
        raise ValueError('Reference outside the source snapshot')
    return path


def source_key(reference):
    return f'{reference["source_id"]}:{reference["start_line"]}-{reference["end_line"]}'
