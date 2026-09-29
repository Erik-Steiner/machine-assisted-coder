"""Cache for sentence-embedding vectors used by the optional embedding-based
classifier engine (see classifier.build_embeddings()). The `embeddings` table
only maps a segment to a row index -- the actual float vectors live in a
single .npy file per model_name under paths.EMBEDDINGS_DIR, since SQLite is a
poor fit for large dense vectors. Mirrors classifier.build_corpus()'s own
"parallel lists in a fixed order" idiom: row_index is just that segment's
position in the .npy matrix, not a source of truth for anything else.

No dependency on classifier.py (which depends on this instead) -- numpy is
already a core dependency (requirements.txt), so this module itself needs no
optional import; only classifier.build_embeddings() touches
sentence_transformers, lazily.
"""
import numpy as np

import paths
from . import schema


def _matrix_path(model_name):
    paths.EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = model_name.replace("/", "__")
    return paths.EMBEDDINGS_DIR / f"{safe_name}.npy"


def filter_missing(segment_ids, texts, model_name):
    """Of the given parallel (segment_ids, texts) lists, return only the pairs
    not yet embedded for model_name, as parallel (missing_ids, missing_texts)."""
    if not segment_ids:
        return [], []
    known = set()
    batch_size = schema.SQLITE_MAX_VARIABLES - 1  # leave room for the model_name param
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), batch_size):
            batch = segment_ids[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""SELECT segment_id FROM embeddings
                    WHERE model_name=? AND segment_id IN ({placeholders})""",
                (model_name, *batch),
            ).fetchall()
            known.update(r["segment_id"] for r in rows)
    missing_ids, missing_texts = [], []
    for sid, text in zip(segment_ids, texts):
        if sid not in known:
            missing_ids.append(sid)
            missing_texts.append(text)
    return missing_ids, missing_texts


def append_embeddings(model_name, new_segment_ids, vectors):
    """Appends vectors (array-like, len == len(new_segment_ids)) to
    model_name's cached matrix on disk, and records each segment's row_index."""
    if not new_segment_ids:
        return
    vectors = np.asarray(vectors, dtype=np.float32)
    matrix_path = _matrix_path(model_name)
    if matrix_path.exists():
        existing = np.load(matrix_path)
        start_row = existing.shape[0]
        combined = np.vstack([existing, vectors])
    else:
        start_row = 0
        combined = vectors
    np.save(matrix_path, combined)

    now = schema._now()
    rows = [(sid, model_name, start_row + i, now) for i, sid in enumerate(new_segment_ids)]
    with schema.get_conn() as conn:
        conn.executemany(
            """INSERT INTO embeddings (segment_id, model_name, row_index, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(segment_id, model_name) DO UPDATE SET
                 row_index=excluded.row_index, created_at=excluded.created_at""",
            rows,
        )


def load_matrix(segment_ids, model_name):
    """Dense numpy array with one row per segment_id, in that exact order --
    must match build_corpus()'s ordering so build_labels()'s y lines up
    row-for-row with this feature matrix. Raises ValueError if any
    segment_id isn't embedded yet for model_name (call
    classifier.build_embeddings() first, which fills any gaps via
    filter_missing()/append_embeddings() before calling this)."""
    matrix_path = _matrix_path(model_name)
    if not matrix_path.exists():
        raise ValueError(f"no cached embeddings yet for model '{model_name}'")
    full_matrix = np.load(matrix_path)

    row_index_by_segment = {}
    batch_size = schema.SQLITE_MAX_VARIABLES - 1
    with schema.get_conn() as conn:
        for i in range(0, len(segment_ids), batch_size):
            batch = segment_ids[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""SELECT segment_id, row_index FROM embeddings
                    WHERE model_name=? AND segment_id IN ({placeholders})""",
                (model_name, *batch),
            ).fetchall()
            row_index_by_segment.update({r["segment_id"]: r["row_index"] for r in rows})

    missing = [sid for sid in segment_ids if sid not in row_index_by_segment]
    if missing:
        raise ValueError(f"{len(missing)} segment(s) not yet embedded for model '{model_name}'")
    return full_matrix[[row_index_by_segment[sid] for sid in segment_ids]]
