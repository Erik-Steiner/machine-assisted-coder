"""Corpus-level analytics: scale/density stats over the same segment corpus
classifier.py actually trains on (see get_corpus_scope()), plus a metadata
breakdown and vocabulary-overlap layer for exploring how that scale/density
shifts across a document-level field (source, person, time, engagement...).

Deliberately pure -- only imports coding_store/classifier, never
viewer_server -- so it stays testable independent of the web layer, same
shape as classifier.py itself. viewer_server.py owns the one thing this
module can't do on its own: joining a segment's dataset_id/item_id to that
document's full metadata record (group_name, publish_date, duration_secs,
...), which lives in the in-memory DATASETS index, not coding.db. Every
function here takes that join's result -- item_meta, a plain
{(dataset_id, item_id): record} dict -- as an argument instead.

Corpus scope deliberately mirrors classifier.build_corpus() exactly (same
interviewer-turn/sub-5-word/near-duplicate/excluded-dataset filters), so
these numbers match what Model tab metrics are actually computed over --
see get_corpus_scope().
"""
import re
import statistics
from collections import Counter
from datetime import datetime

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

import classifier
from coding_store import segments as segments_store

# Same token pattern sklearn's TfidfVectorizer defaults to (2+ word chars,
# unicode-aware) -- vocabulary_size then counts the same units TF-IDF itself
# would vectorize over, not some other arbitrary tokenization.
TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")

TOP_UNIQUE_TERMS_N = 15
MAX_CATEGORICAL_GROUPS = 20  # + an "Other" bucket beyond this, so a high-cardinality
                              # field (e.g. person_name in a large corpus) stays renderable
CATEGORICAL_MAX_DISTINCT = 15  # a numeric field with <=15 distinct values still reads as
                                 # categorical -- e.g. a 1-10 rating scale, per the design brief

# Fields on a document's index record that aren't analytically meaningful as a breakdown
# grouping variable -- identifiers, free text, or (item_title) explicitly called out by the
# design brief as too high-cardinality to group by ("use as a label/identifier, not a
# breakdown grouping variable"). Never hardcodes which fields ARE offered -- see
# discover_fields(), which is driven entirely by what's actually present in the data.
NON_ANALYTICAL_FIELDS = {
    "id", "item_id", "dataset_id", "transcript_text", "turns",
    "source_url", "person_id", "item_title",
}

FIELD_LABELS = {
    "group_name": "Group / organization",
    "person_name": "Person",
    "person_title": "Person title",
    "source_name": "Source",
    "publish_date": "Publish date",
    "duration_secs": "Duration (seconds)",
    "view_count": "View count",
    "like_count": "Likes",
    "combined_classifier_score": "Combined classifier score",
}


def get_corpus_scope(exclude_duplicates=True):
    """The exact segment set classifier.build_corpus() would train on, as full rows (not
    just segment_id/text) -- the single source of truth every function/endpoint below
    shares, so corpus analytics and the classifier are guaranteed to agree on what's in
    scope. Returns (segment_rows, duplicate_run_id, excluded_dataset_ids)."""
    segment_ids, _texts, duplicate_run_id, excluded_dataset_ids = classifier.build_corpus(
        exclude_duplicates=exclude_duplicates
    )
    segment_rows = segments_store.get_segments_by_ids(segment_ids)
    return segment_rows, duplicate_run_id, excluded_dataset_ids


def _tokenize(text, remove_stopwords=False):
    tokens = TOKEN_RE.findall((text or "").lower())
    if remove_stopwords:
        tokens = [t for t in tokens if t not in ENGLISH_STOP_WORDS]
    return tokens


def _coerce_numeric(v):
    """Metadata fields serialize inconsistently across sources -- e.g. this app's own demo
    dataset carries like_count as strings ("3", "62485"). Numeric-looking strings are coerced
    to float so type detection and quantitative breakdowns treat them as numbers, not
    one-bar-per-value categorical noise; anything that doesn't parse cleanly (dates, real
    category labels) passes through unchanged."""
    if isinstance(v, str):
        s = v.strip()
        if s:
            try:
                return float(s)
            except ValueError:
                pass
    return v


def _meta_value(item_meta, row, field):
    return _coerce_numeric((item_meta.get((row["dataset_id"], row["item_id"])) or {}).get(field))


def _filter_rows(segment_rows, item_meta, field, value):
    if field is None:
        return segment_rows
    return [r for r in segment_rows if _meta_value(item_meta, r, field) == value]


def _looks_temporal(values):
    non_null = [v for v in values if v not in (None, "")]
    if not non_null:
        return False
    parsed = 0
    for v in non_null:
        if not isinstance(v, str):
            return False
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            parsed += 1
        except ValueError:
            pass
    return parsed / len(non_null) >= 0.9


def _detect_field_type(values):
    """categorical | temporal | quantitative, per the design brief's Implementation Note: a
    date-shaped string is temporal; a numeric field with few distinct values still reads as
    categorical even though it's stored as a number; everything else numeric is
    quantitative; anything else (including a high-cardinality string) is categorical."""
    if _looks_temporal(values):
        return "temporal"
    non_null = [v for v in values if v not in (None, "")]
    if non_null and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "categorical" if len(set(non_null)) <= CATEGORICAL_MAX_DISTINCT else "quantitative"
    return "categorical"


def _document_field_values(segment_rows, item_meta, field):
    """One value per document (not per segment) for `field`, deduplicated by
    (dataset_id, item_id) -- the unit discover_fields() reasons about."""
    seen = set()
    values = []
    for r in segment_rows:
        key = (r["dataset_id"], r["item_id"])
        if key in seen:
            continue
        seen.add(key)
        values.append(_meta_value(item_meta, r, field))
    return values


def summary_metrics(segment_rows, item_meta=None, field=None, value=None):
    """The six core-metrics tiles (design brief's Core Metrics section), optionally
    restricted to segments whose document's item_meta[...][field] == value (the drill-down
    case triggered by clicking a bar in a categorical breakdown)."""
    rows = _filter_rows(segment_rows, item_meta or {}, field, value)

    documents = {(r["dataset_id"], r["item_id"]) for r in rows}
    word_counts = [r["word_count"] or 0 for r in rows]
    total_word_count = sum(word_counts)

    all_tokens = [t for r in rows for t in _tokenize(r["text"])]
    vocabulary_size = len(set(all_tokens))
    total_tokens = len(all_tokens)
    lexical_diversity = (vocabulary_size / total_tokens) if total_tokens else 0.0

    return {
        "total_documents": len(documents),
        "total_segments": len(rows),
        "total_word_count": total_word_count,
        "vocabulary_size": vocabulary_size,
        "lexical_diversity": lexical_diversity,
        "avg_segment_length": (total_word_count / len(rows)) if rows else 0.0,
        "median_segment_length": statistics.median(word_counts) if word_counts else 0,
        "min_segment_length": min(word_counts) if word_counts else 0,
        "max_segment_length": max(word_counts) if word_counts else 0,
        "showing": {"field": field, "value": value} if field is not None else None,
    }


def discover_fields(segment_rows, item_meta):
    """Candidate breakdown fields -- discovered from whatever keys are actually present on
    this corpus's documents, never hardcoded (a dataset with fields this app has never seen
    before still gets offered). Only fields with at least one non-null value across the
    current corpus scope are included."""
    candidate_fields = set()
    for meta in item_meta.values():
        candidate_fields.update(meta.keys())
    candidate_fields -= NON_ANALYTICAL_FIELDS

    out = []
    for field in sorted(candidate_fields):
        values = _document_field_values(segment_rows, item_meta, field)
        non_null = [v for v in values if v not in (None, "")]
        if not non_null:
            continue
        out.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "type": _detect_field_type(values),
            "n_values": len(set(non_null)),
            "n_missing": len(values) - len(non_null),
        })
    return out


def _bin_granularity(dates):
    span_days = (max(dates) - min(dates)).days
    if span_days <= 60:
        return "day"
    if span_days <= 730:
        return "month"
    return "year"


def _bin_label(dt, granularity):
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "month":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y")


def _categorical_breakdown(doc_rows, field):
    groups = {}
    for d in doc_rows.values():
        label = "Unknown" if d["value"] in (None, "") else str(d["value"])
        g = groups.setdefault(label, {"value": label, "document_count": 0, "segment_count": 0, "word_count": 0})
        g["document_count"] += 1
        g["segment_count"] += d["segment_count"]
        g["word_count"] += d["word_count"]

    ordered = sorted(groups.values(), key=lambda g: -g["document_count"])
    if len(ordered) > MAX_CATEGORICAL_GROUPS:
        head, tail = ordered[:MAX_CATEGORICAL_GROUPS], ordered[MAX_CATEGORICAL_GROUPS:]
        other = {
            "value": "Other", "document_count": sum(g["document_count"] for g in tail),
            "segment_count": sum(g["segment_count"] for g in tail),
            "word_count": sum(g["word_count"] for g in tail),
        }
        ordered = head + [other]
    return {"field": field, "type": "categorical", "groups": ordered}


def _temporal_breakdown(doc_rows, field):
    parsed = {}
    n_missing = 0
    for key, d in doc_rows.items():
        raw = d["value"]
        if not raw:
            n_missing += 1
            continue
        try:
            parsed[key] = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            n_missing += 1

    if not parsed:
        return {"field": field, "type": "temporal", "granularity": None, "groups": [], "n_missing": n_missing}

    granularity = _bin_granularity(list(parsed.values()))
    groups = {}
    for key, dt in parsed.items():
        d = doc_rows[key]
        label = _bin_label(dt, granularity)
        g = groups.setdefault(label, {"value": label, "document_count": 0, "segment_count": 0, "word_count": 0})
        g["document_count"] += 1
        g["segment_count"] += d["segment_count"]
        g["word_count"] += d["word_count"]

    ordered = [groups[k] for k in sorted(groups.keys())]
    return {"field": field, "type": "temporal", "granularity": granularity, "groups": ordered, "n_missing": n_missing}


def field_breakdown(segment_rows, item_meta, field):
    """Categorical: per-value {document_count, segment_count, word_count}, nulls bucketed
    into "Unknown", capped at MAX_CATEGORICAL_GROUPS + an "Other" bucket. Temporal: same
    shape, binned by day/month/year auto-selected from the corpus's date span, nulls
    dropped (n_missing reported). Quantitative: one point per document (dataset_id, item_id,
    field_value, word_count), nulls excluded (n_missing reported) -- scatter, never binned,
    per the design brief."""
    doc_rows = {}
    for r in segment_rows:
        key = (r["dataset_id"], r["item_id"])
        value = _meta_value(item_meta, r, field)
        d = doc_rows.setdefault(key, {"value": value, "word_count": 0, "segment_count": 0})
        d["word_count"] += r["word_count"] or 0
        d["segment_count"] += 1

    values = [d["value"] for d in doc_rows.values()]
    field_type = _detect_field_type(values)

    if field_type == "quantitative":
        points = [
            {"dataset_id": k[0], "item_id": k[1], "field_value": d["value"], "word_count": d["word_count"]}
            for k, d in doc_rows.items() if d["value"] not in (None, "")
        ]
        n_missing = len(doc_rows) - len(points)
        return {"field": field, "type": "quantitative", "points": points, "n_missing": n_missing}

    if field_type == "temporal":
        return _temporal_breakdown(doc_rows, field)

    return _categorical_breakdown(doc_rows, field)


def vocabulary_overlap(segment_rows, item_meta, field):
    """Categorical fields only -- raises ValueError otherwise (callers should check
    discover_fields()'s type first; this re-derives it defensively). Per-group token sets
    using the SAME preprocessing as classifier.py's TfidfVectorizer (lowercase,
    ENGLISH_STOP_WORDS removed) -- the design brief is explicit this must match TF-IDF
    preprocessing or the shared-vocabulary percentage is misleadingly inflated by common
    function words. Returns per group: document_count, shared_pct, unique_pct (of that
    group's own vocabulary), and its top unique terms (present in this group and no other)."""
    raw_values = [_meta_value(item_meta, r, field) for r in segment_rows]
    if _detect_field_type(raw_values) != "categorical":
        raise ValueError(
            f"field '{field}' is not categorical -- vocabulary overlap only applies to categorical breakdowns"
        )

    group_term_counts = {}  # label -> Counter(term -> count), for ranking top unique terms
    group_docs = {}         # label -> set of (dataset_id, item_id)
    for r in segment_rows:
        value = _meta_value(item_meta, r, field)
        label = "Unknown" if value in (None, "") else str(value)
        group_term_counts.setdefault(label, Counter()).update(_tokenize(r["text"], remove_stopwords=True))
        group_docs.setdefault(label, set()).add((r["dataset_id"], r["item_id"]))

    group_vocab = {label: set(counts) for label, counts in group_term_counts.items()}
    labels = list(group_vocab)

    result_groups = []
    for label in labels:
        own = group_vocab[label]
        others = set().union(*(group_vocab[o] for o in labels if o != label)) if len(labels) > 1 else set()
        shared = own & others
        unique = own - others
        total = len(own) or 1  # guard divide-by-zero for a group with an empty vocabulary
        top_unique = sorted(unique, key=lambda t: -group_term_counts[label][t])[:TOP_UNIQUE_TERMS_N]
        result_groups.append({
            "value": label,
            "document_count": len(group_docs[label]),
            "shared_pct": round(100.0 * len(shared) / total, 1),
            "unique_pct": round(100.0 * len(unique) / total, 1),
            "top_unique_terms": top_unique,
        })

    result_groups.sort(key=lambda g: -g["document_count"])
    return {"field": field, "groups": result_groups}
