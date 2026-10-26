from __future__ import annotations
import os
from pathlib import Path
from .common import INDEX, ROOT, digest

DEFAULT_MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
_MODELS = {}


def embedding_model(name, offline=False):
    key = (name, offline)
    if key not in _MODELS:
        from fastembed import TextEmbedding
        cache = str(ROOT / 'models')
        _MODELS[key] = TextEmbedding(model_name=name, cache_dir=cache, threads=4,
                                     local_files_only=offline)
    return _MODELS[key]


def build_vectors(records, name=None):
    import numpy as np
    name = name or os.getenv('RAG_MODEL', DEFAULT_MODEL)
    embedder = embedding_model(name)
    # A multilingual similarity model has a short input window. Embed title/observations separately
    # from each remaining stage, then pool all segments. No silent loss of the correction stage.
    segments = []
    owners = []
    for i, record in enumerate(records):
        texts = [record['title'] + '\n' + ', '.join(record['tags'])]
        p = record['payload']
        if record['kind'] == 'faq':
            texts.append(p['answer'])
        else:
            for key, field in [('observation','signals'),('interpretation','hypotheses'),
                               ('decision','proposed_steps'),('verification','expected_and_checks'),
                               ('correction','on_failed_or_uncertain')]:
                texts.extend(p[key][field])
        for text in texts:
            for start in range(0, len(text), 650):
                segments.append(text[start:start+650])
                owners.append(i)
    vectors = list(embedder.embed(segments, batch_size=16))
    pooled = np.zeros((len(records), len(vectors[0])), dtype='float32')
    counts = np.zeros(len(records), dtype='float32')
    for owner, vector in zip(owners, vectors):
        pooled[owner] += vector
        counts[owner] += 1
    pooled /= counts[:, None]
    pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
    INDEX.mkdir(exist_ok=True)
    np.save(INDEX / 'dense.npy', pooled, allow_pickle=False)
    artifacts = {}
    for path in (ROOT / 'models').rglob('*'):
        if path.is_file() and path.suffix in {'.onnx', '.json'}:
            artifacts[path.relative_to(ROOT).as_posix()] = digest(path)
    return dict(name=name, dimension=int(pooled.shape[1]), segments=len(segments),
                vector_sha256=digest(INDEX / 'dense.npy'), cache_artifacts=artifacts,
                device='CPU', threads=4)


def query_scores(query, meta):
    import numpy as np
    path = INDEX / 'dense.npy'
    if digest(path) != meta['vector_sha256']:
        raise ValueError('Dense index checksum mismatch')
    matrix = np.load(path, allow_pickle=False)
    # Runtime never downloads a missing model. Lexical search remains available if it is absent.
    embedder = embedding_model(meta['name'], offline=True)
    vec = np.asarray(list(embedder.embed([query]))[0], dtype='float32')
    vec /= max(float(np.linalg.norm(vec)), 1e-12)
    return (matrix @ vec).tolist()
