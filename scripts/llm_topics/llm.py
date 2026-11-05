"""One way to call Claude for every stage of the pipeline: structured output into a
Pydantic model, refusal fallback on, and a raw record of each call on disk.

The record (request + response, one JSON file per call) is the provenance for a
methods section: which model, which prompt, which settings produced each output.
It also makes stages resumable -- a call whose record already exists is read back
instead of re-sent (and re-billed), so a crashed or interrupted run picks up
where it stopped. Delete a record to force that call again.

The API key is read from ANTHROPIC_API_KEY in the repo's .env (alongside the
ceointerviews.ai API_KEY/BASE_URL that query_api.py uses) or the environment. It is
never printed or written into a record.
"""
import json
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import common

# Opt-in server-side refusal fallback: if a model's safety classifier declines a
# request, the API re-runs it on Anthropic's recommended fallback model in the same
# call instead of returning a refusal. Interview text about AI safety, misuse and
# bioweapons policy can plausibly trip a classifier.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

load_dotenv(common.ROOT / ".env")


class CallFailed(RuntimeError):
    pass


def client():
    return anthropic.Anthropic(max_retries=4)


def model_settings(model):
    """Per-model request settings. Sonnet/Opus 5.x: effort, no sampling params, refusal
    fallback on. Haiku 4.5 rejects `effort` and has none of the newer refusal
    classifiers the fallback exists for, but does accept temperature -- 0 keeps the
    coding as repeatable as the API allows."""
    if model.startswith("claude-haiku-4-5"):
        return {"effort": None, "fallback": False, "temperature": 0.0}
    return {"effort": "high", "fallback": True, "temperature": None}


def parse_call(record_path, *, model, system, user, output_model, max_tokens=16000,
               cache_system=True, client_=None, **overrides):
    """Send one request (or read its record back) and return (parsed_output, record).

    system: the stable instructions (cached across calls when cache_system).
    user:   the per-call content.
    overrides: effort / fallback / temperature, replacing model_settings(model).
    Raises CallFailed on a refusal or a truncated response -- never returns a
    partial parse."""
    settings = {**model_settings(model), **overrides}
    record_path = Path(record_path)
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        return output_model.model_validate(record["parsed_output"]), record

    system_blocks = [{"type": "text", "text": system}]
    if cache_system:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}
    request = {
        "model": model, "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user}],
    }
    if settings["effort"]:
        request["output_config"] = {"effort": settings["effort"]}
    extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if settings["fallback"] else {}
    if settings["temperature"] is not None:
        # SDK 1.x dropped `temperature` from its signatures; models that still honour it
        # (Haiku 4.5) take it through extra_body, which is merged into the request JSON.
        extra["extra_body"] = {"temperature": settings["temperature"]}
    c = client_ or client()
    # Always stream: the SDK refuses a non-streaming request whose max_tokens could run
    # past its 10-minute HTTP timeout, and long thinking + a large structured answer
    # (the stage 1 reduce call) does. get_final_message() still returns the parsed
    # output, validated against output_model, once the stream ends.
    with c.beta.messages.stream(**request, output_format=output_model, **extra) as stream:
        response = stream.get_final_message()
        request_id = stream.request_id  # a streamed message has no _request_id of its own
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        raise CallFailed(f"{record_path.name}: refused (category={category}), request id {request_id}")
    if response.stop_reason == "max_tokens":
        raise CallFailed(f"{record_path.name}: hit max_tokens={max_tokens}; raise it and re-run")
    if response.parsed_output is None:
        raise CallFailed(f"{record_path.name}: no parsed output (stop_reason={response.stop_reason})")

    record = {
        "request": {**request, "output_format": output_model.__name__, **extra},
        "request_id": request_id,
        "served_by_model": response.model,
        "stop_reason": response.stop_reason,
        "usage": response.usage.to_dict(),
        "parsed_output": response.parsed_output.model_dump(),
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return response.parsed_output, record


def usage_totals(records):
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: sum((r.get("usage") or {}).get(k) or 0 for r in records) for k in keys}
