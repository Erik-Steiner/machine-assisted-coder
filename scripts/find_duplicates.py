"""Detects near-duplicate interviews/speeches in queries/*.json -- the same
real-world speech independently re-transcribed by multiple publications, each
landing with its own item_id. Confirmed concrete case: Dario Amodei's Feb 2026
India AI Summit keynote, re-transcribed near-verbatim by 12 different outlets.
This is a systemic pattern around major press events, not an isolated one --
comparable clusters exist for other frequently-covered speakers too.

Mark-only: this script never deletes, hides, or archives a queries/*.json
record, and never touches codes. It writes detected clusters to coding.db
(duplicate_runs/duplicate_clusters/duplicate_cluster_items -- see
coding_store/schema.py's schema), which classifier.py's build_corpus() then
consults by default to exclude non-canonical duplicates' segments from the
training corpus -- protecting against skewed term weighting, inflated
apparent label support, and cross-validation leakage from replicated
near-identical text.

Detection: items are blocked by (person_name, publish_date within
--window-days), then compared pairwise via whole-document TF-IDF cosine
similarity (a fresh vectorizer fit once per run over every scanned item's
transcript_text -- item-level, not classifier.py's segment-level one; this
matters because it handles a short clip fully contained inside a much
longer full-transcript correctly, which a sequence/edit-distance metric
does not). Pairs scoring >= --threshold are unioned into clusters
(connected components). A cluster's minimum internal pairwise similarity
below threshold means it only holds together via a similarity chain, not
because every member pair is actually close -- flagged low_cohesion for
manual review rather than trusted silently.

Usage:
    python scripts/find_duplicates.py                       # every dataset in queries/_index.json
    python scripts/find_duplicates.py q_87b8c86188 q_abc123  # only these dataset ids
    python scripts/find_duplicates.py --window-days 3 --threshold 0.6
    python scripts/find_duplicates.py --promote-canonical q_87b8c86188:713324
    python scripts/find_duplicates.py --verbose              # also print rejected same-block pairs
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import query_api
from coding_store import activity_log, duplicates, schema
from paths import QUERIES_DIR

WINDOW_DAYS_DEFAULT = 3
THRESHOLD_DEFAULT = 0.6
SHINGLE_SIZE = 8

NGRAM_RANGE = (1, 2)
STOP_WORDS = "english"
MIN_DF = 2


def _parse_date(publish_date):
    """publish_date's first 10 chars (YYYY-MM-DD) -- tolerant of a full
    ISO8601 datetime or a bare date. None if missing/unparseable."""
    if not publish_date:
        return None
    try:
        return date.fromisoformat(publish_date[:10])
    except ValueError:
        return None


def load_items(dataset_ids):
    """Flat list of scannable items across the given (or all) queries/*.json
    datasets: {dataset_id, item_id, person_name, publish_date (date object),
    publish_date_raw, transcript_text, word_count, view_count, source_name}.
    Records missing person_name/publish_date/transcript_text/item_id are
    skipped with a count printed at the end -- they can't be blocked or
    compared (documented v1 limitation)."""
    registry = {e["id"]: e for e in query_api.load_registry(QUERIES_DIR)}
    ids = dataset_ids or list(registry.keys())

    items = []
    skipped = 0
    for dataset_id in ids:
        entry = registry.get(dataset_id)
        if entry is None:
            print(f"skip dataset {dataset_id}: not found in queries/_index.json")
            continue
        json_path = QUERIES_DIR / entry["json_file"]
        if not json_path.exists():
            print(f"skip dataset {dataset_id}: {json_path.name} not found on disk")
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                records = json.load(f)
            query_api.validate_dataset_records(records, json_path.name)
        except (json.JSONDecodeError, query_api.DatasetValidationError) as exc:
            print(f"skip dataset {dataset_id}: {exc}")
            continue

        for rec in records:
            person_name = (rec.get("person_name") or "").strip()
            pub_date = _parse_date(rec.get("publish_date"))
            text = (rec.get("transcript_text") or "").strip()
            item_id = rec.get("item_id")
            if not person_name or pub_date is None or not text or item_id is None:
                skipped += 1
                continue
            items.append({
                "dataset_id": dataset_id, "item_id": str(item_id),
                "person_name": person_name, "publish_date": pub_date,
                "publish_date_raw": rec.get("publish_date"),
                "transcript_text": text, "word_count": len(text.split()),
                "view_count": rec.get("view_count"), "source_name": rec.get("source_name"),
            })

    if skipped:
        print(f"Skipped {skipped} record(s) missing person_name/publish_date/transcript_text/item_id.")
    return items


def word_shingles(text, k=SHINGLE_SIZE):
    words = text.lower().split()
    if len(words) < k:
        return set()
    return {tuple(words[i:i + k]) for i in range(len(words) - k + 1)}


def containment(shingles_a, shingles_b):
    """Fraction of the smaller shingle set found in the larger one -- unlike
    cosine similarity, this directly answers "is A (roughly) a subset of B,"
    which is what canonical-selection reasoning wants to show the researcher."""
    if not shingles_a or not shingles_b:
        return None
    inter = len(shingles_a & shingles_b)
    return inter / min(len(shingles_a), len(shingles_b))


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def find_candidate_pairs(items, window_days):
    """(i, j) index pairs blocked by (person_name, |date diff| <= window_days).
    Items are grouped by exact person_name, then sorted by date so the inner
    loop can break early once the gap exceeds the window."""
    by_person = defaultdict(list)
    for idx, it in enumerate(items):
        by_person[it["person_name"]].append(idx)

    pairs = []
    window = timedelta(days=window_days)
    for idxs in by_person.values():
        idxs.sort(key=lambda i: items[i]["publish_date"])
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if items[j]["publish_date"] - items[i]["publish_date"] > window:
                    break
                pairs.append((i, j))
    return pairs


def build_clusters(items, pairs, sim_fn, threshold):
    """Unions pairs scoring >= threshold; returns ({root_idx: [member_idx,
    ...]} for components of size >= 2, [(i, j, similarity), ...] every
    scored pair -- used for --verbose reporting of rejected candidates)."""
    uf = UnionFind(len(items))
    scored = []
    for i, j in pairs:
        sim = sim_fn(i, j)
        scored.append((i, j, sim))
        if sim >= threshold:
            uf.union(i, j)

    groups = defaultdict(list)
    for idx in range(len(items)):
        groups[uf.find(idx)].append(idx)
    clusters = {root: members for root, members in groups.items() if len(members) >= 2}
    return clusters, scored


def select_canonical(items, member_idxs, coded_counts):
    """Priority: (1) the sole coded member, if exactly one exists; (2) among
    multiple coded members, the longest -- flagged canonical_conflict since
    picking one over another coded member isn't a call this script should
    make silently; (3) longest transcript_text; (4) highest view_count
    tie-break; (5) lexicographically smallest (dataset_id, item_id), for
    reproducible re-runs. Returns (canonical_idx, reason, needs_attention)."""
    def code_count(idx):
        it = items[idx]
        return coded_counts.get((it["dataset_id"], it["item_id"])) or 0

    coded_members = [idx for idx in member_idxs if code_count(idx) > 0]
    if coded_members:
        needs_attention = "canonical_conflict" if len(coded_members) > 1 else None
        canonical = max(coded_members, key=lambda idx: items[idx]["word_count"])
        return canonical, "has_active_codes", needs_attention

    def vc(idx):
        # view_count is occasionally a numeric string (e.g. "65") rather than
        # an int in the raw API data -- coerce defensively, same as app.js's
        # formatNumber() does on the frontend.
        v = items[idx]["view_count"]
        try:
            return float(v) if v is not None else -1
        except (TypeError, ValueError):
            return -1

    best_wc = max(items[idx]["word_count"] for idx in member_idxs)
    wc_ties = [idx for idx in member_idxs if items[idx]["word_count"] == best_wc]
    if len(wc_ties) == 1:
        return wc_ties[0], "longest_text", None

    best_vc = max(vc(idx) for idx in wc_ties)
    vc_ties = [idx for idx in wc_ties if vc(idx) == best_vc]
    if len(vc_ties) == 1:
        return vc_ties[0], "highest_view_count", None

    canonical = min(vc_ties, key=lambda idx: (items[idx]["dataset_id"], items[idx]["item_id"]))
    return canonical, "tiebreak_id", None


def run_duplicate_scan(dataset_ids=None, window_days=WINDOW_DAYS_DEFAULT,
                        threshold=THRESHOLD_DEFAULT, progress_callback=None, actor=None):
    """One full near-duplicate scan: load candidate items, fit one shared
    item-level TF-IDF vectorizer, block+cluster them, and persist the result
    as a new duplicate_runs row. progress_callback(event) fires at each stage
    transition (loading, vectorizing, clustering, cluster_done per cluster) so
    a caller -- this module's CLI, or a web background job -- can report live
    progress without duplicating this function. Mirrors
    classifier.run_training_pass()'s progress_callback shape.

    actor defaults to schema.DEFAULT_CODER if not given (the CLI's caller).

    Returns {run_id, n_items_scanned, n_datasets, clusters: [cluster_row, ...],
    needs_attention_total, coded_flagged_total, params} -- run_id is None only
    when there was nothing to scan (empty items), in which case clusters is [].
    """
    def emit(event):
        if progress_callback:
            progress_callback(event)

    schema.init_db()

    emit({"stage": "loading"})
    items = load_items(dataset_ids)
    if not items:
        return {
            "run_id": None, "n_items_scanned": 0, "n_datasets": 0, "clusters": [],
            "needs_attention_total": 0, "coded_flagged_total": 0, "params": None,
            "items": [], "scored_pairs": [],
        }
    n_datasets = len({it["dataset_id"] for it in items})
    emit({"stage": "loaded", "n_items_scanned": len(items), "n_datasets": n_datasets})

    emit({"stage": "vectorizing", "n_items": len(items)})
    vectorizer = TfidfVectorizer(ngram_range=NGRAM_RANGE, stop_words=STOP_WORDS, min_df=MIN_DF)
    X = vectorizer.fit_transform([it["transcript_text"] for it in items])

    def sim(i, j):
        return float(cosine_similarity(X[i], X[j])[0, 0])

    pairs = find_candidate_pairs(items, window_days)

    emit({"stage": "clustering", "n_candidate_pairs": len(pairs)})
    clusters, scored = build_clusters(items, pairs, sim, threshold)

    shingles = [word_shingles(it["transcript_text"]) for it in items]

    all_pairs = [
        (items[idx]["dataset_id"], items[idx]["item_id"])
        for members in clusters.values() for idx in members
    ]
    coded_counts = duplicates.get_active_code_counts(all_pairs)

    run_id = f"dup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    created_at = datetime.now(timezone.utc).isoformat()
    cluster_rows = []
    needs_attention_total = 0
    coded_flagged_total = 0

    sorted_roots = sorted(
        clusters.keys(),
        key=lambda root: min((items[idx]["dataset_id"], items[idx]["item_id"]) for idx in clusters[root]),
    )
    for cluster_id, root in enumerate(sorted_roots, start=1):
        member_idxs = sorted(clusters[root], key=lambda idx: (items[idx]["dataset_id"], items[idx]["item_id"]))

        # Honest cohesion: full pairwise among the FINAL cluster members, not
        # just the union-find edges that happened to link them -- catches
        # the transitivity risk of a similarity chain (A~B~C, A!~C).
        pair_sims = [
            sim(member_idxs[a], member_idxs[b])
            for a in range(len(member_idxs)) for b in range(a + 1, len(member_idxs))
        ]
        min_sim, mean_sim = min(pair_sims), sum(pair_sims) / len(pair_sims)

        canonical_idx, reason, conflict_flag = select_canonical(items, member_idxs, coded_counts)

        member_rows = []
        cluster_coded_flagged = 0
        for idx in member_idxs:
            it = items[idx]
            is_canonical = idx == canonical_idx
            sim_to_canon = 1.0 if is_canonical else sim(idx, canonical_idx)
            cont_to_canon = 1.0 if is_canonical else containment(shingles[idx], shingles[canonical_idx])
            member_rows.append({
                "dataset_id": it["dataset_id"], "item_id": it["item_id"],
                "is_canonical": is_canonical,
                "similarity_to_canonical": sim_to_canon,
                "containment_in_canonical": cont_to_canon,
                "word_count": it["word_count"], "view_count": it["view_count"],
                "source_name": it["source_name"], "publish_date": it["publish_date_raw"],
            })
            if not is_canonical and (coded_counts.get((it["dataset_id"], it["item_id"])) or 0) > 0:
                cluster_coded_flagged += 1

        needs_attention = conflict_flag
        if needs_attention is None and min_sim < threshold:
            needs_attention = "low_cohesion"
        if needs_attention is None and cluster_coded_flagged:
            needs_attention = "coded_non_canonical"
        if needs_attention:
            needs_attention_total += 1
        coded_flagged_total += cluster_coded_flagged

        cluster_row = {
            "cluster_id": cluster_id,
            "person_name": items[member_idxs[0]]["person_name"],
            "size": len(member_idxs),
            "min_pairwise_similarity": min_sim,
            "mean_pairwise_similarity": mean_sim,
            "canonical_dataset_id": items[canonical_idx]["dataset_id"],
            "canonical_item_id": items[canonical_idx]["item_id"],
            "canonical_reason": reason,
            "needs_attention": needs_attention,
            "members": member_rows,
        }
        cluster_rows.append(cluster_row)
        emit({"stage": "cluster_done", "index": cluster_id, "total": len(sorted_roots), "cluster": cluster_row})

    params = {
        "window_days": window_days, "threshold": threshold,
        "ngram_range": list(NGRAM_RANGE), "min_df": MIN_DF, "stop_words": STOP_WORDS,
        "shingle_size": SHINGLE_SIZE, "n_items_scanned": len(items),
    }
    duplicates.save_duplicate_run(run_id, created_at, "tfidf_cosine_v1", params, cluster_rows)
    activity_log.log_activity(actor or schema.DEFAULT_CODER, "duplicate_detection_run", {
        "run_id": run_id, "n_items_scanned": len(items), "n_clusters": len(cluster_rows),
        "needs_attention": needs_attention_total,
    })

    return {
        "run_id": run_id, "n_items_scanned": len(items), "n_datasets": n_datasets,
        "clusters": cluster_rows, "needs_attention_total": needs_attention_total,
        "coded_flagged_total": coded_flagged_total, "params": params,
        # items/scored_pairs: not needed by the web job, only by main()'s --verbose
        # report below (which pairs, below threshold, would have been rejected).
        "items": items, "scored_pairs": scored,
    }


def _print_progress(event):
    stage = event["stage"]
    if stage == "loading":
        print("Scanning items...")
    elif stage == "loaded":
        print(f"{event['n_items_scanned']} scannable item(s) across {event['n_datasets']} dataset(s).")
    elif stage == "vectorizing":
        print(f"Fitting item-level TF-IDF (ngram {NGRAM_RANGE}, min_df={MIN_DF})...")
    elif stage == "clustering":
        print(f"{event['n_candidate_pairs']} candidate pair(s) to compare.")
    elif stage == "cluster_done":
        c = event["cluster"]
        members = c["members"]
        canon = next(m for m in members if m["is_canonical"])
        dates = sorted(m["publish_date"] or "" for m in members)
        print(f"[{c['person_name']}] cluster {event['index']} of {event['total']}, size {c['size']}, "
              f"{dates[0]} to {dates[-1]} "
              f"(min/mean pairwise sim: {c['min_pairwise_similarity']:.2f}/{c['mean_pairwise_similarity']:.2f})")
        print(f"  canonical: {canon['dataset_id']}:{canon['item_id']} "
              f"({canon['source_name'] or '?'}, {canon['word_count']}w, reason={c['canonical_reason']})")
        for m in members:
            if m["is_canonical"]:
                continue
            print(f"    {m['dataset_id']}:{m['item_id']}  {(m['source_name'] or '?'):<24} "
                  f"{m['word_count']}w  sim={m['similarity_to_canonical']:.3f}")
        if c["needs_attention"]:
            print(f"  NEEDS ATTENTION: {c['needs_attention']}")
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset_ids", nargs="*",
                         help="restrict to these dataset ids (default: every dataset in queries/_index.json)")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS_DEFAULT,
                         help=f"same-person blocking window in days (default {WINDOW_DAYS_DEFAULT})")
    parser.add_argument("--threshold", type=float, default=THRESHOLD_DEFAULT,
                         help=f"TF-IDF cosine similarity threshold to cluster a pair (default {THRESHOLD_DEFAULT})")
    parser.add_argument("--promote-canonical", action="append", default=[], metavar="dataset_id:item_id",
                         help="make this item the canonical member of its cluster in the latest run "
                              "(repeatable); skips detection entirely")
    parser.add_argument("--verbose", action="store_true",
                         help="also print rejected same-block candidate pairs and their scores")
    args = parser.parse_args()

    # source_name/item_title values in queries/*.json can contain characters
    # outside Windows' default console codepage (cp1252) -- printing one
    # would otherwise crash this report outright, especially when stdout is
    # redirected to a file rather than a real terminal. Best-effort report:
    # replace what can't be displayed rather than lose the whole run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    schema.init_db()

    if args.promote_canonical:
        run_id = duplicates.get_latest_duplicate_run_id()
        if run_id is None:
            print("No duplicate detection run exists yet -- run this script without --promote-canonical first.")
            sys.exit(1)
        for spec in args.promote_canonical:
            if ":" not in spec:
                print(f"--promote-canonical expects dataset_id:item_id, got {spec!r}")
                sys.exit(1)
            dataset_id, item_id = spec.split(":", 1)
            try:
                duplicates.set_duplicate_cluster_canonical(run_id, dataset_id, item_id)
                print(f"{dataset_id}:{item_id} is now the canonical member of its cluster in run {run_id}.")
            except ValueError as exc:
                print(f"skip {spec}: {exc}")
        return

    summary = run_duplicate_scan(
        dataset_ids=args.dataset_ids, window_days=args.window_days,
        threshold=args.threshold, progress_callback=_print_progress,
    )
    if summary["run_id"] is None:
        print("No scannable items found.")
        return

    if args.verbose:
        items, scored = summary["items"], summary["scored_pairs"]
        rejected = sorted((s for s in scored if s[2] < args.threshold), key=lambda s: -s[2])
        if rejected:
            print(f"-- {len(rejected)} rejected candidate pair(s) below threshold (top 50 by score) --")
            for i, j, s in rejected[:50]:
                print(f"  {items[i]['dataset_id']}:{items[i]['item_id']} <-> "
                      f"{items[j]['dataset_id']}:{items[j]['item_id']}  sim={s:.3f}")
            print()

    print(f"{len(summary['clusters'])} cluster(s), {summary['needs_attention_total']} flagged NEEDS ATTENTION, "
          f"{summary['coded_flagged_total']} non-canonical member(s) with active codes across all clusters.")
    print(f"Run id: {summary['run_id']}")
    print(f"Query: SELECT * FROM duplicate_clusters WHERE run_id='{summary['run_id']}'")
    print("\nscripts/train_classifiers.py will now exclude non-canonical duplicates' segments "
          "from training by default. Pass --include-duplicates to opt out for one run.")


if __name__ == "__main__":
    main()
