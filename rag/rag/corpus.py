from __future__ import annotations
import json
from .common import DATA, INDEX, digest, read_json, source_path, write_json

PRIMARY_FILES = ['faq_curated.json', 'situations.json', 'conflicts.json', 'api_constraints.json']


def fingerprint():
    return {f'knowledge_base/{p}': digest(DATA / 'knowledge_base' / p) for p in PRIMARY_FILES} | {
        'sources/register.json': digest(DATA / 'sources/register.json'),
        'sources/sections.json': digest(DATA / 'sources/sections.json'),
        'APPROVAL.json': digest(DATA / 'APPROVAL.json')}


def approved_records():
    records = []
    for filename, kind in [('faq_curated.json', 'faq'), ('situations.json', 'situation')]:
        for item in read_json(DATA / 'knowledge_base' / filename):
            if item.get('review_status') != 'APPROVED':
                continue
            title = item.get('question', item.get('title'))
            if kind == 'faq':
                body = item['answer']
                tags = item.get('tags', [])
            else:
                # Stage citations are stored once in the record; no repeated duplicate text in the index.
                body = '\n'.join(item['observation']['signals'] + item['interpretation']['hypotheses']
                                 + item['decision']['proposed_steps'] + item['verification']['expected_and_checks']
                                 + item['correction']['on_failed_or_uncertain'])
                tags = item.get('nodes', []) + [item['category']]
            records.append(dict(id=item['id'], kind=kind, title=title, text=body, tags=tags,
                                scope=item['scope'], authority='approved_advisory', payload=item,
                                conflict_ids=item.get('conflict_ids', []), source_refs=item['source_refs']))
    if not records:
        raise ValueError('No approved records available')
    return records


def reference_records():
    sections = read_json(DATA / 'sources' / 'sections.json')
    by_file = {}
    for entry in sections:
        ref = entry['source_refs'][0]
        by_file.setdefault(ref['path'], []).append(entry)
    records = []
    for path, entries in by_file.items():
        entries.sort(key=lambda x: x['source_refs'][0]['start_line'])
        lines = source_path(entries[0]['source_refs'][0]).read_text(encoding='utf-8-sig').splitlines()
        for i, item in enumerate(entries):
            ref = dict(item['source_refs'][0])
            # Index each line once under its nearest heading, not repeated parent sections.
            stop = entries[i+1]['source_refs'][0]['start_line'] - 1 if i+1 < len(entries) else len(lines)
            ref['end_line'] = stop
            body = '\n'.join(lines[ref['start_line']-1:stop]).strip()
            if len(body) < 70:
                continue
            records.append(dict(id='REF-' + item['id'], kind='reference', title=item['title'], text=body,
                                tags=[ref['source_id']], scope='reference_only', authority='unreviewed_source',
                                source_refs=[ref], conflict_ids=[]))
    return records


def validate_sources():
    for source in read_json(DATA / 'sources' / 'register.json'):
        if digest(DATA / source['path']) != source['sha256']:
            raise ValueError('Source changed: ' + source['path'] + '; rebuild and re-review dependent material')


def build(semantic=False, model=None):
    validate_sources()
    records = approved_records()
    references = reference_records()
    meta = dict(version=1, fingerprint=fingerprint(), approved_records=len(records),
                reference_records=len(references), semantic=False,
                raw_reference_policy='search-only; never used as standalone operational answers')
    write_json(INDEX / 'records.json', records)
    write_json(INDEX / 'references.json', references)
    if semantic:
        from .dense import build_vectors
        meta['semantic_model'] = build_vectors(records, model)
        meta['semantic'] = True
    write_json(INDEX / 'metadata.json', meta)
    return meta
