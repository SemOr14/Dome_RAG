from __future__ import annotations
import collections
import math
import re
from .common import DATA, INDEX, read_json
from .corpus import fingerprint, validate_sources

STOP = set('и в во на за к по с со о об от до из у а но или для это как что чем ли же не нет да при если то чтобы только какой какие почему можно нужно делать после перед его она они он мы вы их все еще уже который которых такое такая быть есть без через когда'.split())
STOP.update('кто те сколько точно любой сейчас сегодня году прошлом'.split())
SUFFIXES = sorted('иями ями ами ого ему ому ыми ими иях ах ях ие ые ое ей ий ый ой ая яя юю ую ость ости ения ение ений ать ять ить ывать ивать еть еют ует ют ет ют ся сь ов ев ам ям ом ем а я ы и у ю е о'.split(), key=len, reverse=True)
ALIASES = {
    'чпу': 'cnc', 'фрезер': 'cnc', 'фрезерный': 'cnc', 'фрезеровка': 'cnc',
    'принтер': 'printer', 'печат': 'printer', 'печать': 'printer', 'напечатать': 'printer',
    'углекислый': 'co2', 'углекислого': 'co2', 'carbon': 'co2',
    'готово': 'completed', 'закончилась': 'completed', 'завершилась': 'completed',
    'приток': 'вентиляция', 'аккумулятор': 'батарея', 'акб': 'батарея',
    'датчиков': 'датчик', 'станков': 'станок', 'влажность': 'rh',
    'лампы': 'освещение', 'лампа': 'освещение', 'свет': 'освещение',
    'моргают': 'мигание', 'мигает': 'мигание', 'мигают': 'мигание', 'скачут': 'шум',
    'продолжить': 'resume', 'продолжать': 'resume', 'возобновить': 'resume',
    'машина': 'станок', 'машины': 'станок',
}


def normalize(text):
    return text.lower().replace('ё','е').replace('₂','2').replace('→',' ')


def tokens(text):
    out = []
    for term in re.findall(r'[a-zа-я0-9_]+', normalize(text)):
        if term in STOP or len(term) < 2:
            continue
        term = ALIASES.get(term, term)
        if re.fullmatch('[а-я]+', term) and len(term) > 4:
            for suffix in SUFFIXES:
                if term.endswith(suffix) and len(term)-len(suffix) >= 4:
                    term = term[:-len(suffix)]
                    break
        out.append(term)
    return out


class SearchEngine:
    def __init__(self, semantic=True, verify_sources=True):
        self.meta = read_json(INDEX / 'metadata.json')
        if self.meta['fingerprint'] != fingerprint():
            raise ValueError('Knowledge changed after index build; run build again')
        if verify_sources:
            validate_sources()
        self.records = read_json(INDEX / 'records.json')
        self.references = read_json(INDEX / 'references.json')
        self.semantic = semantic and self.meta.get('semantic', False)
        self.last_semantic_error = None
        self._stats = {}

    def _bm25(self, query, records):
        marker = id(records)
        if marker not in self._stats:
            docs = [collections.Counter(tokens(r['title'])*4 + tokens(' '.join(r['tags']))*2 + tokens(r['text'])) for r in records]
            df = collections.Counter(t for doc in docs for t in doc)
            lengths = [sum(doc.values()) for doc in docs]
            self._stats[marker] = (docs, df, lengths, sum(lengths)/max(len(lengths),1))
        docs, df, lengths, avg = self._stats[marker]
        terms = set(tokens(query))
        result = []
        for doc, length in zip(docs, lengths):
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)
                if tf:
                    idf = math.log(1 + (len(docs)-df[term]+0.5)/(df[term]+0.5))
                    score += idf * (tf*2.2)/(tf+1.2*(0.25+0.75*length/avg))
            result.append(score)
        return result

    def search(self, query, limit=5, kind=None, include_reference=False):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
            raise ValueError('Question must contain 1..4000 characters')
        if kind not in {None, 'faq', 'situation', 'reference'}:
            raise ValueError('Unknown record kind')
        if not 1 <= limit <= 20:
            raise ValueError('limit must be 1..20')
        records = self.references if kind == 'reference' else self.records
        if kind == 'reference' and not include_reference:
            raise ValueError('Raw reference search must be explicitly enabled')
        lexical = self._bm25(query, records)
        dense = None
        self.last_semantic_error = None
        if self.semantic and records is self.records:
            try:
                from .dense import query_scores
                dense = query_scores(query, self.meta['semantic_model'])
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                self.last_semantic_error = type(exc).__name__
        max_lex = max(lexical, default=0) or 1
        lex_ranks = {idx:rank+1 for rank,idx in enumerate(sorted(range(len(records)),key=lambda i:-lexical[i])) if lexical[idx]>0}
        dense_ranks = {idx:rank+1 for rank,idx in enumerate(sorted(range(len(records)),key=lambda i:-dense[i]))} if dense is not None else {}
        results = []
        query_terms = set(tokens(query))
        printer_question = bool(re.search(r'принтер|печат|сопл|webhook|printer',normalize(query)))
        cnc_question = bool(re.search(r'чпу|фрезер|probe_z|wco|cnc',normalize(query)))
        for i, r in enumerate(records):
            if kind and r['kind'] != kind:
                continue
            title_terms = set(tokens(r['title'] + ' ' + ' '.join(r['tags'])))
            coverage = len(query_terms & title_terms)/max(len(query_terms),1)
            lex = lexical[i]/max_lex
            sem = dense[i] if dense is not None else 0.0
            # Similarity is only a ranking signal; it is never reported as diagnostic confidence.
            # Weighted reciprocal ranks let semantic matches participate even without shared words.
            score = (0.45*6/(5+lex_ranks[i]) if i in lex_ranks else 0) + 0.50*6/(5+dense_ranks[i]) + 0.05*coverage if dense is not None else 0.85*lex + 0.15*coverage
            nodes = set(r.get('payload',{}).get('nodes',[]))
            cnc_specific = ('cnc_01' in nodes and 'printer_3d_01' not in nodes) or bool(re.search(r'probe_z|нул[ья]|чпу',normalize(r['title'])))
            printer_specific = ('printer_3d_01' in nodes and 'cnc_01' not in nodes) or bool(re.search(r'принтер|печат',normalize(r['title'])))
            if printer_question and not cnc_question and cnc_specific:
                score *= 0.5
            if cnc_question and not printer_question and printer_specific:
                score *= 0.5
            if re.search(r'(?<![A-Za-z0-9])'+re.escape(r['id'])+r'(?![A-Za-z0-9])', query, re.I):
                score += 2
            if lexical[i] <= 0 and (dense is None or sem < 0.35):
                continue
            results.append(dict(id=r['id'], kind=r['kind'], title=r['title'], score=round(score,6),
                                lexical_score=round(lexical[i],4), semantic_similarity=round(sem,4) if dense is not None else None,
                                title_coverage=round(coverage,3), authority=r['authority'],
                                source_refs=r['source_refs'], conflict_ids=r['conflict_ids']))
        results.sort(key=lambda r: (-r['score'], r['id']))
        return results[:limit]

    def record(self, ident):
        return next((r for r in self.records if r['id']==ident), None)
