"""Renders the Web Appendix tab's activity feed (coding_store.get_appendix_feed())
into one self-contained HTML file: no external CSS/JS/fonts, so it opens
correctly from disk with no server running. A researcher gets a PDF for free
via the browser's own "Print to PDF" on the rendered page -- no PDF library
needed here.
"""
import html
import json
from datetime import datetime, timezone

ACTION_LABELS = {
    "theme_create": "Theme created",
    "theme_update": "Theme edited",
    "theme_archive": "Theme archived",
    "theme_restore": "Theme restored",
    "theme_merge": "Themes merged",
    "training_run": "Classifier training run",
    "query_download": "Data downloaded",
    "codebook_export": "Codebook exported",
    "filter_snapshot": "Browse filter snapshot",
}


def _summarize(action_type, details):
    """One human-readable line per row; falls back to the raw action_type for
    anything not in ACTION_LABELS (e.g. a future action type this version of
    the exporter doesn't know about yet)."""
    if action_type == "theme_create":
        return f'Created theme "{details.get("name", "?")}"'
    if action_type == "theme_update":
        fields = ", ".join(details.get("fields", {}).keys()) or "no fields"
        return f'Edited theme "{details.get("theme_id", "?")}" ({fields})'
    if action_type == "theme_archive":
        return f'Archived theme "{details.get("theme_id", "?")}"'
    if action_type == "theme_restore":
        return f'Restored theme "{details.get("theme_id", "?")}"'
    if action_type == "theme_merge":
        src = details.get("source_theme_id_name", details.get("source_theme_id", "?"))
        dst = details.get("target_theme_id_name", details.get("target_theme_id", "?"))
        return f'Merged "{src}" into "{dst}"'
    if action_type == "training_run":
        trained = ", ".join(t["name"] for t in details.get("trained", [])) or "none"
        return f'Trained model {details.get("model_version", "?")} -- themes: {trained}'
    if action_type == "query_download":
        return f'Downloaded "{details.get("label", "?")}" ({details.get("count", "?")} interviews)'
    if action_type == "codebook_export":
        return f'Exported codebook ({", ".join(details.get("files", []))})'
    if action_type == "filter_snapshot":
        return "Browse filters active at export/citation time"
    return ACTION_LABELS.get(action_type, action_type)


def render_html(rows):
    generated_at = datetime.now(timezone.utc).isoformat()
    body_rows = []
    for row in rows:
        label = ACTION_LABELS.get(row["action_type"], row["action_type"])
        summary = html.escape(_summarize(row["action_type"], row["details"]))
        details_json = html.escape(_pretty(row["details"]))
        body_rows.append(f"""
        <tr>
          <td class="ts">{html.escape(row["ts"])}</td>
          <td>{html.escape(row["actor"])}</td>
          <td><span class="badge">{html.escape(label)}</span></td>
          <td>{summary}
            <details><summary>details</summary><pre>{details_json}</pre></details>
          </td>
        </tr>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Research Activity Log</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 2rem;
          color: #1a1a1a; background: #fff; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .meta {{ color: #555; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem 0.6rem; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f4; }}
  .ts {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .badge {{ display: inline-block; background: #eef0fb; color: #33408f; border-radius: 4px;
            padding: 0.1rem 0.5rem; font-size: 0.78rem; }}
  details {{ margin-top: 0.3rem; }}
  pre {{ white-space: pre-wrap; word-break: break-word; background: #fafafa; padding: 0.5rem;
         border-radius: 4px; font-size: 0.78rem; }}
  @media print {{ body {{ margin: 0.5in; }} }}
</style>
</head>
<body>
  <h1>Research Activity Log</h1>
  <p class="meta">Generated {html.escape(generated_at)} &mdash; {len(rows)} recorded action(s).
    Exported from Machine Assisted Coder's Web Appendix tab. Use your browser's
    "Print to PDF" to produce a PDF version of this page for a manuscript's supplementary
    material.</p>
  <table>
    <thead><tr><th>Timestamp (UTC)</th><th>Actor</th><th>Action</th><th>Summary</th></tr></thead>
    <tbody>{"".join(body_rows) if body_rows else '<tr><td colspan="4">No recorded actions yet.</td></tr>'}</tbody>
  </table>
</body>
</html>
"""


def _pretty(details):
    return json.dumps(details, indent=2, ensure_ascii=False)
