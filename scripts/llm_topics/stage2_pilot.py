"""Stage 2 of the LLM topic-coding pipeline: pilot the coding prompt on interviews
with known answers, before spending on the full corpus.

Coded with CODING_MODEL and exactly the prompt the full run will use (coding.py):
  gold    all 19 read-in-full interviews -- every turn has a researcher label for each
          seed theme, so precision AND recall can be measured
  random  RANDOM_PER_COMPANY other interviews per company -- what the coding looks like
          on ordinary interviews, including the emergent topics (which have no gold)

What it measures, for each seed theme, on gold turns that weren't prompt examples:
  - precision / recall / F1 of "the theme is the primary or secondary topic", with 95%
    Wilson intervals (the gold sets are small: 19-102 positives per theme)
  - the old TF-IDF classifier on the same turns (out-of-fold score >= its threshold), as
    a baseline the LLM has to beat
  - how often a seed-theme primary assignment is right, by confidence band (calibration)
And for every topic: how often it is primary / secondary, and how much ends up `other`.

Precision is a lower bound until the researcher adjudicates the disagreements: some
"false positives" will be passages the researcher missed. disagreements.csv lists every
one, with a blank `researcher_verdict` column (llm_right / gold_right / unclear).

Outputs (exports/llm_topics/stage2/):
  codebook_used.json   the exact codebook coded against (codebook_version)
  coded_pilot.csv      one row per executive turn in the pilot interviews -- the deliverable's columns
  metrics.json, pilot_report.md, disagreements.csv
  raw/                 one record per API call (resumable; delete to re-run a call)

Usage:
    python scripts/llm_topics/stage2_pilot.py           # dry run: requests, prompt, cost estimate
    python scripts/llm_topics/stage2_pilot.py --run
"""
import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd

import coding
import common

RANDOM_PER_COMPANY = 3
MIN_TURNS_RANDOM = 10
SEED = 20261007
WORKERS = 6
MAX_TOKENS = 12000
CONFIDENCE_BANDS = [0, 0.5, 0.7, 0.85, 1.0001]

# Haiku 4.5 list prices, $/million tokens; for the estimate and the usage summary.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 1.00, 5.00, 0.10, 1.25

OUT_COLUMNS = ["comment_id", "interview_id", "turn_id", "company", "speaker_name", "primary_topic_id",
               "secondary_topic_id", "confidence", "topic_summary", "justification", "coded_by",
               "codebook_version"]


def pick_interviews(turns):
    gold = sorted(turns.loc[turns["gold_set"] == "read_in_full", "interview_id"].unique())
    per = turns[turns["coded_scope"] == "llm"].groupby(["interview_id", "company", "gold_set"]).size()
    per = per.rename("n").reset_index()
    pool = per[(per["gold_set"] != "read_in_full") & (per["n"] >= MIN_TURNS_RANDOM)]
    rand = (pool.groupby("company", group_keys=False)
            .apply(lambda g: g.sample(min(RANDOM_PER_COMPANY, len(g)), random_state=SEED)))
    return gold, sorted(rand["interview_id"])


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(centre - half, 3), round(centre + half, 3))


def prf(pred, gold):
    tp = int((pred & gold).sum()); fp = int((pred & ~gold).sum()); fn = int((~pred & gold).sum())
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if p == p and r == r and p + r else float("nan")
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 3), "precision_ci": wilson(tp, tp + fp),
            "recall": round(r, 3), "recall_ci": wilson(tp, tp + fn), "f1": round(f, 3)}


def run_requests(requests, system, out_model, topic_ids, version, out_dir):
    import llm
    client = llm.client()

    def call(req, suffix=""):
        key, _, ids, user = req
        return llm.parse_call(out_dir / "raw" / f"{key}{suffix}.json", model=common.CODING_MODEL,
                              system=system, user=user, output_model=out_model,
                              max_tokens=MAX_TOKENS, client_=client)

    results = [call(requests[0])]  # first alone: writes the system-prompt cache for the rest
    with ThreadPoolExecutor(WORKERS) as pool:
        results += list(pool.map(call, requests[1:]))

    rows, records, problems = {}, [r for _, r in results], []
    retry = []
    for req, (parsed, _) in zip(requests, results):
        got, missing, notes = coding.check_codes(parsed, req[2])
        rows.update(got)
        problems += [f"{req[0]}: {n}" for n in notes]
        if missing:
            retry.append((req, missing))
    for req, missing in retry:  # one retry, asking only for what's missing
        key, iid, ids, user = req
        user = user.rsplit("\n\nCode these", 1)[0] + \
            f"\n\nCode these {len(missing)} turns, each exactly once: {', '.join(missing)}"
        parsed, rec = call((key, iid, missing, user), suffix="_retry")
        records.append(rec)
        got, still, notes = coding.check_codes(parsed, missing)
        rows.update(got)
        problems += [f"{key} retry: {n}" for n in notes]
        if still:
            problems.append(f"{key}: still missing after retry: {still}")
    coded = pd.DataFrame(rows.values())
    coded["coded_by"] = common.CODING_MODEL
    coded["codebook_version"] = version
    return coded, records, problems


def evaluate(coded, turns, topics, thresholds):
    seed_ids = [t["topic_id"] for t in topics if t["origin"] == "seed"]
    names = {t["topic_id"]: t["name"] for t in topics}
    prompt_examples = {c for t in topics for c in t["example_comment_ids"] + t["near_miss_comment_ids"]}
    df = coded.merge(turns, on="comment_id", how="left", suffixes=("", "_t"))
    ev = df[(df["gold_set"] == "read_in_full") & (df["coded_by"] != "rule") & ~df["eval_exclude"]
            & ~df["comment_id"].isin(prompt_examples)]

    per_theme, disagreements = {}, []
    for th in seed_ids:
        sub = ev[ev[f"gold_{th}"].isin(["pos", "neg"])]
        gold = sub[f"gold_{th}"] == "pos"
        llm_any = (sub["primary_topic_id"] == th) | (sub["secondary_topic_id"] == th)
        llm_primary = sub["primary_topic_id"] == th
        # a theme from the app's codebook export may be newer than the seed pack's
        # classifier: no threshold, so no TF-IDF baseline (all False)
        tfidf = sub[f"score_{th}"] >= thresholds.get(th, float("inf"))
        per_theme[th] = {
            "name": names[th], "n_turns": int(len(sub)), "n_gold_pos": int(gold.sum()),
            "llm_primary_or_secondary": prf(llm_any, gold),
            "llm_primary_only": prf(llm_primary, gold),
            "tfidf_baseline": prf(tfidf.fillna(False), gold),
        }
        for idx in sub.index[llm_any != gold]:
            r = sub.loc[idx]
            disagreements.append({
                "theme": th, "theme_name": names[th],
                "type": "llm_yes_gold_no" if llm_any[idx] else "llm_no_gold_yes",
                "comment_id": r["comment_id"], "company": r["company"], "speaker_name": r["speaker_name"],
                "llm_primary": r["primary_topic_id"], "llm_secondary": r["secondary_topic_id"],
                "confidence": r["confidence"], "justification": r["justification"],
                "researcher_verdict": "", "body": r["body"],
            })

    # calibration: seed-theme primary assignments on gold turns, right or wrong by band
    cal_rows = []
    for th in seed_ids:
        s = ev[(ev["primary_topic_id"] == th) & ev[f"gold_{th}"].isin(["pos", "neg"])]
        cal_rows.append(pd.DataFrame({"confidence": s["confidence"], "correct": s[f"gold_{th}"] == "pos"}))
    cal = pd.concat(cal_rows) if cal_rows else pd.DataFrame(columns=["confidence", "correct"])
    cal["band"] = pd.cut(cal["confidence"], CONFIDENCE_BANDS, right=False)
    calibration = [{"band": str(b), "n": int(len(g)), "share_correct": round(g["correct"].mean(), 3) if len(g) else None}
                   for b, g in cal.groupby("band", observed=False)]

    model_rows = df[df["coded_by"] != "rule"]
    dist = []
    for t in topics:
        p = int((df["primary_topic_id"] == t["topic_id"]).sum())
        s = int((df["secondary_topic_id"] == t["topic_id"]).sum())
        dist.append({"topic_id": t["topic_id"], "name": t["name"], "origin": t["origin"],
                     "primary": p, "secondary": s,
                     "primary_share": round(p / len(df), 3) if len(df) else 0})
    summary = {
        "turns_coded": int(len(df)), "by_model": int(len(model_rows)), "by_rule": int((df["coded_by"] == "rule").sum()),
        "with_secondary": int((model_rows["secondary_topic_id"] != "none").sum()),
        "mean_confidence": round(model_rows["confidence"].mean(), 3),
        "eval_turns": int(len(ev)),
    }
    return per_theme, calibration, dist, summary, pd.DataFrame(disagreements)


def write_report(out_dir, summary, per_theme, calibration, dist, problems, usage, cost, version, gold_ids, rand_ids):
    L = [
        "# Stage 2 pilot report", "",
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Model `{common.CODING_MODEL}`, codebook `{version}`, "
        f"seed themes prioritized for the two slots: {coding.PRIORITIZE_SEED_THEMES}.", "",
        f"- Interviews: {len(gold_ids)} read-in-full (gold) + {len(rand_ids)} random.",
        f"- Executive turns coded: {summary['turns_coded']:,} ({summary['by_model']:,} by the model, "
        f"{summary['by_rule']:,} by rule as non_substantive). With a secondary topic: {summary['with_secondary']:,}. "
        f"Mean confidence {summary['mean_confidence']}.",
        f"- Gold turns evaluated (read-in-full, not prompt examples or their twins): {summary['eval_turns']:,}.",
        f"- API usage: {usage}; about ${cost:.2f}.", "",
        "## Seed themes against the researcher's codes", "",
        "\"LLM\" = the theme is the primary or secondary topic. Precision is a lower bound until "
        "`disagreements.csv` is adjudicated. TF-IDF = out-of-fold score at or above the classifier's "
        "threshold (that threshold was tuned on in-sample scores, so this baseline is rough).", "",
        "| theme | gold pos / turns | LLM precision [95% CI] | LLM recall [95% CI] | LLM F1 | primary-only recall | TF-IDF P / R / F1 |",
        "|---|---|---|---|---|---|---|",
    ]
    for th, m in per_theme.items():
        a, p1, b = m["llm_primary_or_secondary"], m["llm_primary_only"], m["tfidf_baseline"]
        L.append(f"| {m['name']} | {m['n_gold_pos']} / {m['n_turns']:,} | {a['precision']} {list(a['precision_ci'])} "
                 f"| {a['recall']} {list(a['recall_ci'])} | {a['f1']} | {p1['recall']} "
                 f"| {b['precision']} / {b['recall']} / {b['f1']} |")
    L += ["", "## Confidence calibration", "",
          "Seed-theme primary assignments on gold turns: share that the researcher's codes agree with, by confidence band.", "",
          "| confidence | n | share correct |", "|---|---|---|"]
    L += [f"| {c['band']} | {c['n']} | {c['share_correct']} |" for c in calibration]
    L += ["", "## Topic distribution (all pilot turns)", "", "| topic | origin | primary | share | secondary |",
          "|---|---|---|---|---|"]
    L += [f"| {d['name']} (`{d['topic_id']}`) | {d['origin']} | {d['primary']} | {d['primary_share']:.1%} | {d['secondary']} |"
          for d in sorted(dist, key=lambda d: -d["primary"])]
    L += ["", "## Response problems", ""] + ([f"- {p}" for p in problems] or ["None."])
    L += ["", "## Next", "",
          "1. Adjudicate `disagreements.csv` (`researcher_verdict`: llm_right / gold_right / unclear). "
          "llm_right rows are passages the original coding missed.",
          "2. Read a sample of `coded_pilot.csv` for the random interviews: are emergent topics and "
          "`other` used sensibly?",
          "3. Revise definitions / include / exclude / near misses where errors cluster, re-run stage 0 if the "
          "seed codebook changed, then re-run this stage (changed codebook = new version = fresh calls)."]
    (out_dir / "pilot_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main(run=False):
    started = datetime.now(timezone.utc)
    turns = pd.read_parquet(common.STAGE0_DIR / "turns_prepared.parquet")
    topics, version = coding.load_codebook(turns)
    topic_ids = [t["topic_id"] for t in topics]
    gold_ids, rand_ids = pick_interviews(turns)
    requests = coding.build_requests(turns, gold_ids + rand_ids)
    system = coding.system_prompt(topics)

    out_dir = common.STAGE2_DIR / version  # a changed codebook never mixes with an old run's records
    out_dir.mkdir(parents=True, exist_ok=True)
    common.write_json(out_dir / "codebook_used.json", {"codebook_version": version, "topics": topics,
                                                        "prioritize_seed_themes": coding.PRIORITIZE_SEED_THEMES})
    (out_dir / "system_prompt.txt").write_text(system, encoding="utf-8")
    (out_dir / "user_example.txt").write_text(requests[0][3], encoding="utf-8")

    n_targets = sum(len(r[2]) for r in requests)
    sys_tok = len(system) / 4
    user_tok = sum(len(r[3]) for r in requests) / 4
    est = (sys_tok * PRICE_CACHE_WRITE + sys_tok * (len(requests) - 1) * PRICE_CACHE_READ
           + user_tok * PRICE_IN + n_targets * 110 * PRICE_OUT) / 1e6
    print(f"codebook {version}: {len(topics)} topics "
          f"({sum(t['origin'] == 'seed' for t in topics)} seed, {sum(t['origin'] == 'emergent' for t in topics)} emergent, 2 fixed)")
    stale = coding.stale_references(topics)
    for s in stale:
        print(f"  check: {s}")
    if stale:
        print(f"  {len(stale)} possible stale topic reference(s) -- fix them in the codebook files before --run")
    for s in coding.shared_examples(topics):
        print(f"  check: {s} -- fine if the turn carries both, a mixed signal if they're alternatives")
    print(f"pilot: {len(gold_ids)} gold + {len(rand_ids)} random interviews, {n_targets:,} turns to code "
          f"in {len(requests)} requests")
    print(f"system prompt ~{sys_tok:,.0f} tokens (cached), requests ~{user_tok:,.0f} tokens; estimated cost ~${est:.2f}")
    if not run:
        print(f"dry run: prompts in {out_dir} (system_prompt.txt, user_example.txt). Re-run with --run.")
        return

    out_model = coding.output_model(topic_ids)
    coded, records, problems = run_requests(requests, system, out_model, topic_ids, version, out_dir)
    coded = pd.concat([coded, coding.rule_rows(turns, gold_ids + rand_ids, version)], ignore_index=True)
    meta = turns[["comment_id", "interview_id", "turn_id", "company", "speaker_name"]]
    coded = coded.merge(meta, on="comment_id", how="left")[OUT_COLUMNS]
    coded.to_csv(out_dir / "coded_pilot.csv", index=False, encoding="utf-8-sig")

    seed_pack = common.load_seed_pack()
    thresholds = {t["theme_id"]: t["threshold"] for t in seed_pack}
    per_theme, calibration, dist, summary, dis = evaluate(coded, turns, topics, thresholds)
    dis.to_csv(out_dir / "disagreements.csv", index=False, encoding="utf-8-sig")

    import llm
    usage = llm.usage_totals(records)
    cost = (usage["input_tokens"] * PRICE_IN + usage["output_tokens"] * PRICE_OUT
            + usage["cache_read_input_tokens"] * PRICE_CACHE_READ
            + usage["cache_creation_input_tokens"] * PRICE_CACHE_WRITE) / 1e6
    common.write_json(out_dir / "metrics.json", {
        "codebook_version": version, "model": common.CODING_MODEL, "run_at": started.isoformat(timespec="seconds"),
        "gold_interviews": [int(i) for i in gold_ids], "random_interviews": [int(i) for i in rand_ids],
        "summary": summary, "seed_themes": per_theme, "calibration": calibration, "topic_distribution": dist,
        "problems": problems, "usage": usage, "approx_cost_usd": round(cost, 2),
    })
    write_report(out_dir, summary, per_theme, calibration, dist, problems, usage, cost, version, gold_ids, rand_ids)
    print(f"coded {len(coded):,} turns; {len(dis)} disagreements; about ${cost:.2f}. Report: {out_dir / 'pilot_report.md'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="store_true", help="call the API (default is a dry run)")
    main(run=parser.parse_args().run)
