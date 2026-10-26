from .common import DATA, ROOT, read_json


def evaluate(engine):
    cases = read_json(DATA/'evaluation/acceptance_cases.json')
    extras = read_json(ROOT/'evaluation/paraphrases.json')
    sets = [('design_cases',cases),('paraphrases',extras)]
    summaries = {}
    for name,rows in sets:
        outcomes=[]
        for row in rows:
            expected=set(row.get('expected_situation_ids',row.get('expected_ids',[])))
            if not expected:
                continue
            hits=engine.search(row['query'],limit=5)
            ids=[x['id'] for x in hits]
            ranks=[ids.index(x)+1 for x in expected if x in ids]
            outcomes.append(dict(id=row['id'],query=row['query'],expected_ids=sorted(expected),
                                 retrieved_ids=ids,hit_at_5=bool(ranks),reciprocal_rank=1/min(ranks) if ranks else 0))
        summaries[name]=dict(cases=len(outcomes),hit_rate_at_5=sum(x['hit_at_5'] for x in outcomes)/max(len(outcomes),1),
                             mean_reciprocal_rank=sum(x['reciprocal_rank'] for x in outcomes)/max(len(outcomes),1),cases_detail=outcomes)
    return dict(scope='retrieval_only_not_llm_factual_or_equipment_validation',
                semantic_index=engine.semantic,semantic_fallback_error=engine.last_semantic_error,
                metric_note='Hit@5: at least one expected ID in first five. Title-based design cases are easier than independent paraphrases.',
                datasets=summaries)
