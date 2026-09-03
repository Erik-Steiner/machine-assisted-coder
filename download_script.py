"""Bulk-downloads transcripts for one or more companies from the
ceointerviews.ai API, one query export per company, written into queries/ in
the exact same JSON+CSV format and registered in the same queries/_index.json
that viewer_server.py's Search & Export tab uses. Run this to build a
starting dataset; anything it downloads shows up in the Browse tab's dataset
dropdown immediately, same as a query run by hand.

Company names are resolved to company_id via the live get_companies keyword
search -- the same lookup the Search & Export tab's UI uses -- so nothing
about which companies to fetch is hardcoded here; this script works for
whichever companies your own research targets.

Run:
    python download_script.py "Company Name" "Another Company"

With no arguments, reads company names (one per line, blank lines and
'#'-prefixed lines ignored) from companies.txt next to this script, if
present -- see companies.example.txt for the format.
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import query_api
from paths import QUERIES_DIR

HERE = Path(__file__).parent
COMPANIES_FILE = HERE / "companies.txt"
CHECKPOINT_EVERY = 1  # rewrite this company's export after every N executives


def _company_label(result):
    return result.get("name") or result.get("full_name") or f"company_id {result.get('company_id')}"


def resolve_company(name):
    """Look up a company name against the live API. Returns (company_id,
    matched_name) on a confident match, or None (after printing why) if the
    name doesn't resolve to exactly one company."""
    results = query_api.search_companies(name)
    if not results:
        print(f'  no match for "{name}" -- skipping')
        return None
    exact = [r for r in results if _company_label(r).lower() == name.lower()]
    match = exact[0] if len(exact) == 1 else results[0] if len(results) == 1 else None
    if match is None:
        print(f'  "{name}" matched {len(results)} companies -- rerun with the exact name:')
        for r in results:
            print(f"    {_company_label(r)}  (company_id {r.get('company_id')})")
        return None
    return match["company_id"], _company_label(match)


def fetch_company(company_name, company_id):
    executives = query_api.api_get("get_entities", company_id=company_id, page_size=500)["results"]
    print(f"{company_name}: {len(executives)} executives")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"company_{query_api.slugify(company_name)}_{stamp}"

    rows = []
    for i, ex in enumerate(executives, start=1):
        print(f"  {ex['name']:<25} {ex['title']}")
        try:
            items = query_api.fetch_feed(entity_id=ex["id"])
        except Exception as exc:
            print(f"    FAILED: {exc}")
            continue

        rows.extend(query_api.build_row(item, company_name) for item in items)
        print(f"    {len(items)} interviews")

        if i % CHECKPOINT_EVERY == 0:
            query_api.write_export(QUERIES_DIR, base_name, query_api.dedupe_rows(rows))

    rows = query_api.dedupe_rows(rows)
    json_path, csv_path = query_api.write_export(QUERIES_DIR, base_name, rows)
    query_api.register_export(
        QUERIES_DIR,
        dataset_id=f"q_{uuid.uuid4().hex[:10]}",
        label=f"{company_name} — {len(rows)} interviews",
        kind="company",
        source_id=company_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        count=len(rows),
        json_path=json_path,
        csv_path=csv_path,
    )
    print(f"  Saved {len(rows)} rows to {json_path.name} / {csv_path.name}\n")
    return rows


def load_company_names(argv):
    if argv:
        return argv
    if COMPANIES_FILE.exists():
        lines = COMPANIES_FILE.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    return []


def main():
    names = load_company_names(sys.argv[1:])
    if not names:
        print('Usage: python download_script.py "Company Name" ["Another Company" ...]')
        print(f"(or list company names, one per line, in {COMPANIES_FILE.name} -- "
              f"see companies.example.txt)")
        sys.exit(1)

    total = 0
    companies_done = 0
    for name in names:
        print(f'Looking up "{name}"...')
        resolved = resolve_company(name)
        if resolved is None:
            continue
        company_id, matched_name = resolved
        rows = fetch_company(matched_name, company_id)
        total += len(rows)
        companies_done += 1
    print(f"Done. {total} interviews across {companies_done} companies, saved into {QUERIES_DIR}/")


if __name__ == "__main__":
    main()
