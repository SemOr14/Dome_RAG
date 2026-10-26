"""Import the approved design as a versioned runtime dataset, preserving source files."""
from pathlib import Path
import hashlib
import json
import shutil

root = Path(__file__).resolve().parents[1]
review = root.parent / 'outputs' / 'dome-rag-project-20261026'
data = root / 'data'
data.mkdir(exist_ok=True)
for directory in ['knowledge_base', 'sources', 'evaluation']:
    shutil.copytree(review / directory, data / directory, dirs_exist_ok=True)
for filename in ['faq_curated.json', 'situations.json', 'conflicts.json']:
    path = data / 'knowledge_base' / filename
    rows = json.loads(path.read_text(encoding='utf-8'))
    for row in rows:
        row['review_status'] = 'APPROVED'
        if filename != 'conflicts.json':
            row['retrieval_policy'] = 'approved_for_advisory_retrieval'
    if filename == 'faq_curated.json':
        rows[0]['answer'] = ('RAG выполняет локальный поиск по FAQ и ситуациям, собирает источники и конфликты, '
            'возвращает пять этапов разбора. Опциональный DeepSeek формулирует диагностическое предложение. '
            'Права, исполнение команд и фактическая проверка оборудования остаются отдельными функциями агента; '
            'исполнителя оборудования в RAG нет.')
        command = next(row for row in rows if row['id']=='F03')
        command['answer'] += (' Единого времени ожидания выполнения любой команды нет: оно зависит от операции, '
            'новой телеметрии и фактической задержки. probe_z может длиться дольше таймаута приёма команды; '
            'истечение времени само по себе не означает, что действие не было выполнено.')
        command['tags'] += ['ожидание','таймаут','секунды','время выполнения команды']
        probe = next(row for row in rows if row['id']=='F23')['source_refs'][0]
        command['source_refs'].append(probe)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
(data / 'APPROVAL.json').write_text(json.dumps({
    'date': '2026-10-26', 'user_instruction': 'собирай',
    'scope': 'Implementation of approved RAG design and advisory knowledge retrieval; not live equipment control.',
    'approved_ids': {'faq': 'F01-F32', 'situations': 'S01-S24', 'conflicts': 'C01-C12'},
    'unreviewed_source_candidates': 'reference_only',
    'editorial_update': 'F01 describes the runtime. F03 clarifies operation-specific waiting and asynchronous probe_z, citing API 7.6.'
}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('Imported approved KB and all 21 source snapshots; original design preserved.')
