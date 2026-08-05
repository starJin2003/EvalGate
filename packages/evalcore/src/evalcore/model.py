"""The real `ModelClient`: llama-server over its OpenAI-compatible endpoint.

Deliberately stdlib-only. `evalcore` has exactly one dependency (pydantic) and a
single POST to a localhost endpoint does not justify a second. It also means the
harness image stays small and arm64-clean, which matters because this ships to the
OCI Ampere node in P2.

Three things this must get right, all of them established by measurement in P1.3
rather than assumed:

  - The prompt comes from `evalcore.prompt`, the same function that rendered the
    training data. Re-deriving it here is how eval scores drift with nothing
    erroring.
  - `enable_thinking: false` is passed explicitly. Qwen3's template only inserts
    the empty `<think></think>` block when asked, and the training targets all
    carry it. It happened to work without the flag on the probe model, but that
    relies on learned behaviour rather than on the prompt matching.
  - A prompt over the server's context returns HTTP 400 `exceed_context_size_error`
    and NOT a truncated answer. That is raised as an exception so `run_case`
    records it as a per-case error. A silently truncated prompt would produce a
    confident wrong answer that scores as a genuine one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .prompt import build_messages
from .schema import Case


class ModelError(RuntimeError):
    """Anything that makes one case unanswerable. `run_case` catches it and records
    the case as errored, so one bad case never aborts a suite."""


class ContextOverflowError(ModelError):
    """The rendered prompt did not fit the server's context window.

    Its own type because it is the one failure that is a *suite* problem rather
    than a model problem: it means `-c` is set below what retrieval produces, and
    the fix is server configuration, not a retry.
    """

    def __init__(self, message: str, n_prompt_tokens: int = 0, n_ctx: int = 0) -> None:
        super().__init__(message)
        self.n_prompt_tokens = n_prompt_tokens
        self.n_ctx = n_ctx


@dataclass
class LlamaServerModel:
    """Implements the `ModelClient` protocol against llama-server.

    `ref` identifies the weights *and* the quantization, because "v1" alone does
    not distinguish a Q4_K_M run from an f16 one and the diff report labels runs
    with it.
    """

    version: str
    quantization: str = "Q4_K_M"
    base_url: str = "http://127.0.0.1:8080"
    timeout_s: float = 180.0
    max_tokens: int = 512
    temperature: float = 0.0
    enable_thinking: bool = False
    extra_body: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"llama-server:{self.version}:{self.quantization}"

    def build_info(self) -> str | None:
        """llama.cpp's own build string, read from /props.

        Recorded in the run artifact so a diff can refuse to compare runs served
        by different llama.cpp builds. It is OBSERVED rather than declared, which
        is the point: the backend label is supplied by the caller and could be
        wrong, while this comes from the server. Returns None rather than raising
        -- failing a whole run because a provenance field was unavailable would
        be the wrong trade.
        """
        try:
            request = urllib.request.Request(f"{self.base_url}/props", method="GET")
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read()).get("build_info")
        except Exception:
            return None

    def _payload(self, case: Case) -> dict:
        return {
            "messages": build_messages(case.question, case.context),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # Named `chat_template_kwargs` by llama.cpp; forwarded into the jinja
            # template, which is where Qwen3 decides on the empty think block.
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
            **self.extra_body,
        }

    def generate(self, case: Case) -> str:
        body = json.dumps(self._payload(case)).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise self._from_http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"llama-server unreachable at {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ModelError(f"llama-server timed out after {self.timeout_s}s") from exc

        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"unexpected response shape: {json.dumps(payload)[:200]}") from exc

    @staticmethod
    def _from_http_error(exc: urllib.error.HTTPError) -> ModelError:
        raw = exc.read().decode(errors="replace")
        try:
            error = json.loads(raw).get("error", {})
        except json.JSONDecodeError:
            error = {}
        message = error.get("message", raw[:200])
        if error.get("type") == "exceed_context_size_error":
            return ContextOverflowError(
                f"prompt does not fit the server context: {message}",
                n_prompt_tokens=int(error.get("n_prompt_tokens", 0)),
                n_ctx=int(error.get("n_ctx", 0)),
            )
        return ModelError(f"llama-server HTTP {exc.code}: {message}")
