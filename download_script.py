import time
import requests
import pandas as pd
from urllib.parse import urlencode
from dotenv import load_dotenv
import os

load_dotenv()

api_key = os.getenv("API_KEY")
base_url = os.getenv("BASE_URL")

if not api_key or not base_url:
    raise RuntimeError(
        "API_KEY and BASE_URL must be set (found in .env). "
        "Check that .env sits next to this script and defines both."
    )
base_url = base_url.rstrip("/")

OUTPUT_JSON = "executive_interviews.json"
CHECKPOINT_EVERY = 1  # rewrite OUTPUT_JSON after every N executives, so a crash mid-run keeps partial progress

# ticker -> company_id (company_id is what get_entities/get_feed actually filter on)
tickers = {"None-Anthropic": 613, "None-OpenAI": 612, "None-Google LLC": 4183}


def api_get(endpoint, **params):
    """GET one API page. Retries transient errors — large transcript pages can
    occasionally time out server-side, so never assume a single try succeeds."""
    url = f"{base_url}/api/{endpoint}/?{urlencode(params)}"
    for attempt in range(4):
        try:
            resp = requests.get(url, headers={"X-API-Key": api_key}, timeout=90)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
        except requests.exceptions.RequestException:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)  # 1s, 2s, 4s between retries
    raise RuntimeError(f"{endpoint} kept returning 5xx: {url}")


def fetch_all_feed_for_entity(entity_id, page_size=500):
    """Keyset-paginate get_feed for one entity_id, returning every interview record they appear in."""
    items = []
    last_seen_id = None
    while True:
        params = {"entity_id": entity_id, "page_size": page_size}
        if last_seen_id is not None:
            params["before_feed_item_id"] = last_seen_id
        data = api_get("get_feed", **params)
        items.extend(data.get("results", []))
        last_seen_id = data.get("last_seen_id")
        if not data.get("page_has_next", False) or last_seen_id is None:
            break
    return items


def build_transcript_text(item):
    """Plain, speaker-labeled text for NLP use. Prefers enhanced_transcript
    (already clean speaker turns); falls back to parsing the raw SRT blocks
    for older items that lack it."""
    segments = item.get("enhanced_transcript") or []
    if segments:
        lines = [
            f"[{seg.get('speaker_name', 'unknown')}] {seg['content'].strip()}"
            for seg in segments
            if seg.get("content")
        ]
        return "\n".join(lines)

    raw_blocks = item.get("transcript") or []
    words = []
    for block in raw_blocks:
        # each block is "index\nHH:MM:SS,mmm --> HH:MM:SS,mmm\ncaption text"
        parts = block.split("\n")
        caption = " ".join(parts[2:]) if len(parts) > 2 else ""
        if caption.strip():
            words.append(caption.strip())
    return " ".join(words)


def build_row(executive, item):
    extra = item.get("extra") or {}
    return {
        "company": executive["company"],
        "executive": executive["executive"],
        "title": executive["title"],
        "entity_id": executive["entity_id"],
        "entity_name": item.get("entity_name"),
        "entity_title": item.get("entity_title"),
        "feed_item_id": item.get("feed_item_id"),
        "item_title": item.get("item_title"),
        "source_url": item.get("source_url"),
        "publish_date": item.get("publish_date"),
        "duration_secs": extra.get("duration_secs"),
        "view_count": extra.get("view_count"),
        "like_count": extra.get("like_count"),
        "combined_classifier_score": extra.get("combined_classifier_score"),
        "channel_name": extra.get("channel_name"),
        # Clean, ready-to-analyze text — the main payload for a downstream NLP pipeline.
        "transcript_text": build_transcript_text(item),
        # Full-fidelity speaker/timestamp structure, kept as native JSON (list of dicts).
        "enhanced_transcript_json": item.get("enhanced_transcript") or [],
    }


def save_progress(rows, path):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["entity_id", "feed_item_id"])
    df["publish_date"] = pd.to_datetime(df["publish_date"], utc=True, format="ISO8601", errors="coerce")
    df["year"] = df["publish_date"].dt.year
    df.to_json(path, orient="records", indent=2, date_format="iso", force_ascii=False)
    return df


csuites = {}
executives = []

for tick in tickers:
    csuites[tick] = api_get("get_entities", company_id=tickers[tick], page_size=500)["results"]

for tick, entities in csuites.items():
    print(f"{tick}: {len(entities)} executives")
    company_name = tick.replace("None-", "")
    for e in entities:
        print(f"  {e['name']:<25} {e['title']}")
        executives.append({
            "company": company_name,
            "entity_id": e["id"],
            "executive": e["name"],
            "title": e["title"],
        })

feed_rows = []
for i, ex in enumerate(executives, start=1):
    try:
        feed = fetch_all_feed_for_entity(ex["entity_id"])
    except requests.exceptions.RequestException as exc:
        print(f"  {ex['company']:<10} {ex['executive']:<22} FAILED: {exc}")
        continue

    for item in feed:
        feed_rows.append(build_row(ex, item))
    print(f"  {ex['company']:<10} {ex['executive']:<22} {len(feed)} interviews")

    if i % CHECKPOINT_EVERY == 0:
        save_progress(feed_rows, OUTPUT_JSON)

feed_df = save_progress(feed_rows, OUTPUT_JSON)
print(f"\nSaved {len(feed_df)} rows to {OUTPUT_JSON}")
print(f"Rows with transcript text: {(feed_df['transcript_text'].str.len() > 0).sum()}")
