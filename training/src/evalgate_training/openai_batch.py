"""Thin wrapper over the OpenAI Batch API.

Submit / poll / fetch are separate so a 24-hour completion window survives across
sessions. Batch state is persisted to artifacts/batch_state.json keyed by job name.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

from . import config


def client() -> OpenAI:
    return OpenAI(api_key=config.openai_api_key())


def chat_request(custom_id: str, model: str, messages: list[dict[str, Any]], schema: dict) -> dict:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema["name"], "strict": True, "schema": schema["schema"]},
            },
        },
    }


# --- state -------------------------------------------------------------------
def _load_state() -> dict[str, Any]:
    if config.BATCH_STATE_FILE.exists():
        return json.loads(config.BATCH_STATE_FILE.read_text())
    return {}


def _save_state(state: dict[str, Any]) -> None:
    config.BATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.BATCH_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def record_batch(job: str, batch_id: str, request_count: int) -> None:
    state = _load_state()
    state[job] = {"batch_id": batch_id, "requests": request_count, "status": "submitted"}
    _save_state(state)


def batch_id_for(job: str) -> str:
    state = _load_state()
    if job not in state:
        raise RuntimeError(f"no batch recorded for job {job!r}. Submit it first.")
    return state[job]["batch_id"]


def jobs_matching(prefix: str) -> list[str]:
    return sorted(j for j in _load_state() if j.startswith(prefix))


# --- lifecycle ---------------------------------------------------------------
def submit(job: str, requests: list[dict], workdir: Path) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    payload = workdir / f"{job}_input.jsonl"
    with payload.open("w") as fh:
        for r in requests:
            fh.write(json.dumps(r) + "\n")

    api = client()
    with payload.open("rb") as fh:
        uploaded = api.files.create(file=fh, purpose="batch")
    batch = api.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"job": job},
    )
    record_batch(job, batch.id, len(requests))
    print(f"submitted {len(requests)} requests as batch {batch.id} (job={job})")
    return batch.id


def poll(job: str, wait: bool = False, interval: int = 60) -> str:
    api = client()
    bid = batch_id_for(job)
    while True:
        batch = api.batches.retrieve(bid)
        counts = batch.request_counts
        print(
            f"{job}: {batch.status}  "
            f"completed={getattr(counts, 'completed', 0)} "
            f"failed={getattr(counts, 'failed', 0)} "
            f"total={getattr(counts, 'total', 0)}"
        )
        if batch.status in {"completed", "failed", "expired", "cancelled"} or not wait:
            return batch.status
        time.sleep(interval)


def fetch(job: str) -> list[dict]:
    """Download results. Returns parsed JSONL lines; raises if the batch is unfinished."""
    api = client()
    batch = api.batches.retrieve(batch_id_for(job))
    if batch.status != "completed":
        raise RuntimeError(f"batch {batch.id} is {batch.status}, not completed")
    if batch.error_file_id:
        errors = api.files.content(batch.error_file_id).text
        print(f"WARNING: batch reported errors:\n{errors[:2000]}")
    content = api.files.content(batch.output_file_id).text
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def parse_result(line: dict) -> tuple[str, dict | None, int, int, str | None]:
    """-> (custom_id, parsed_json_body, prompt_tokens, completion_tokens, error)."""
    custom_id = line["custom_id"]
    if line.get("error"):
        return custom_id, None, 0, 0, str(line["error"])
    resp = line["response"]
    if resp.get("status_code") != 200:
        return custom_id, None, 0, 0, f"status {resp.get('status_code')}"
    body = resp["body"]
    usage = body.get("usage", {})
    try:
        parsed = json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, json.JSONDecodeError) as exc:
        return custom_id, None, 0, 0, f"unparseable content: {exc}"
    return (
        custom_id,
        parsed,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        None,
    )
