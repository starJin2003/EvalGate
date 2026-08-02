"""LlamaServerModel against a fake llama-server.

Why a real socket rather than monkeypatching `urlopen`: the failure modes worth
catching are at the HTTP boundary. A patched function would let a wrong method, a
missing Content-Type, a non-JSON body or an unread error stream pass silently.
`http.server` on port 0 costs milliseconds and exercises the actual request the
client will send.

Why this fake is faithful enough to catch a real integration break: **the payloads
are transcripts, not inventions.** Both were captured from llama-server b10210
serving Qwen3-1.7B Q4_K_M during P1.3 -- the success envelope with its
`choices[0].message.content` / `usage` / `timings` keys, and the overflow body

    {"error":{"code":400,"message":"request (12009 tokens) exceeds the available
     context size (8192 tokens), try increasing it",
     "type":"exceed_context_size_error","n_prompt_tokens":12009,"n_ctx":8192}}

verbatim, including the `type` string the client branches on. If llama.cpp renames
that field the fake stops matching reality -- so these tests are pinned to a
server version, and re-capturing on upgrade is the maintenance cost.

What it does NOT cover, and what must be checked against a live server before
trusting a run: real tokenization and the true context arithmetic, streaming,
concurrent slots, and the chat template actually honouring `enable_thinking`.
This suite proves the client speaks the protocol; it cannot prove the server
behaves as recorded.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evalcore.model import ContextOverflowError, LlamaServerModel, ModelError
from evalcore.prompt import SYSTEM
from evalcore.runner import ModelClient, run_case
from evalcore.schema import Case, Category, ContextChunk, ScorerKind, ScorerSpec

OVERFLOW_BODY = {
    "error": {
        "code": 400,
        "message": (
            "request (12009 tokens) exceeds the available context size "
            "(8192 tokens), try increasing it"
        ),
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 12009,
        "n_ctx": 8192,
    }
}


def _success(content: str) -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1889, "completion_tokens": 104, "total_tokens": 1993},
        "timings": {"prompt_per_second": 970.2, "predicted_per_second": 84.0},
    }


class FakeServer:
    """Serves one scripted reply and records the request it received."""

    def __init__(self, status: int, body: dict) -> None:
        self.received: dict | None = None
        self.path: str | None = None
        self.content_type: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's required name
                length = int(self.headers.get("Content-Length", 0))
                outer.received = json.loads(self.rfile.read(length))
                outer.path = self.path
                outer.content_type = self.headers.get("Content-Type")
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> FakeServer:
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def make_case() -> Case:
    return Case(
        case_id="q-0001",
        category=Category.factual,
        question="What are the provider values?",
        context=[
            ContextChunk(
                label="C1",
                chunk_id="aaaa000000000001",
                repo="grafana",
                heading_path="Configure > external_image_storage",
                source_url="https://example.test/cfg",
                content="provider can be s3, webdav, gcs, azure_blob, or local.",
            )
        ],
        scorers=[ScorerSpec(kind=ScorerKind.citation)],
    )


def test_satisfies_the_model_client_protocol() -> None:
    assert isinstance(LlamaServerModel(version="v1"), ModelClient)


def test_ref_carries_version_and_quantization() -> None:
    assert LlamaServerModel(version="v1").ref == "llama-server:v1:Q4_K_M"
    assert LlamaServerModel(version="v2", quantization="f16").ref == "llama-server:v2:f16"


def test_returns_message_content_and_sends_the_trained_prompt() -> None:
    with FakeServer(200, _success("The provider can be s3 [C1].")) as server:
        model = LlamaServerModel(version="v1", base_url=server.url)
        assert model.generate(make_case()) == "The provider can be s3 [C1]."

    sent = server.received
    assert server.path == "/v1/chat/completions"
    assert server.content_type == "application/json"
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    # The system turn must be the trained one, not a paraphrase.
    assert sent["messages"][0]["content"] == SYSTEM
    # And the user turn must carry the rendered chunk in the trained shape.
    assert (
        "[C1] repo=grafana | Configure > external_image_storage" in sent["messages"][1]["content"]
    )
    assert sent["messages"][1]["content"].endswith("Question: What are the provider values?")


def test_temperature_is_zero_and_thinking_is_explicitly_disabled() -> None:
    with FakeServer(200, _success("x")) as server:
        LlamaServerModel(version="v1", base_url=server.url).generate(make_case())
    assert server.received["temperature"] == 0.0
    assert server.received["chat_template_kwargs"] == {"enable_thinking": False}


def test_context_overflow_raises_a_typed_error_carrying_both_counts() -> None:
    with FakeServer(400, OVERFLOW_BODY) as server:
        model = LlamaServerModel(version="v1", base_url=server.url)
        with pytest.raises(ContextOverflowError) as caught:
            model.generate(make_case())
    assert caught.value.n_prompt_tokens == 12009
    assert caught.value.n_ctx == 8192


def test_overflow_becomes_a_case_error_not_a_suite_crash() -> None:
    """The integration point that matters: `run_case` already catches client
    exceptions, so one oversized prompt must cost one case, not the run."""
    with FakeServer(400, OVERFLOW_BODY) as server:
        model = LlamaServerModel(version="v1", base_url=server.url)
        result = run_case(make_case(), model)
    assert result.output == ""
    assert "ContextOverflowError" in result.error
    assert "12009" in result.error


def test_other_http_errors_are_model_errors_not_overflow() -> None:
    with FakeServer(500, {"error": {"message": "internal", "type": "server_error"}}) as server:
        model = LlamaServerModel(version="v1", base_url=server.url)
        with pytest.raises(ModelError) as caught:
            model.generate(make_case())
    assert not isinstance(caught.value, ContextOverflowError)
    assert "500" in str(caught.value)


def test_unreachable_server_is_a_model_error() -> None:
    model = LlamaServerModel(version="v1", base_url="http://127.0.0.1:1", timeout_s=2.0)
    with pytest.raises(ModelError, match="unreachable"):
        model.generate(make_case())


def test_malformed_success_body_is_reported_not_swallowed() -> None:
    with FakeServer(200, {"choices": []}) as server:
        model = LlamaServerModel(version="v1", base_url=server.url)
        with pytest.raises(ModelError, match="unexpected response shape"):
            model.generate(make_case())
