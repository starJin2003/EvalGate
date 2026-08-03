"""`evalgate-eval run --timeout-s` reaches the client.

Why this flag exists: `LlamaServerModel.timeout_s` defaults to 180s, which is
sized for the Metal-backed local server where the whole 96-case suite ran at
5.12 s/case. CPU-only inference on the OCI Ampere node is roughly an order of
magnitude slower and prefill-dominated, and the suite's longest rendered prompt
is ~7.1k tokens. A case that runs past the timeout is recorded by `run_case` as
a per-case error, which in the run artifact is indistinguishable from the model
failing to answer -- so a too-low timeout does not merely slow the run, it
silently manufactures the exact signal the gate is built to detect.

The test asserts the value actually reaches the constructed client, not just
that argparse accepts it. Wiring a flag to nothing is the failure mode worth
covering: `--server-url` itself sat unused for a whole phase because the CLI
could not construct `LlamaServerModel` at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalcore import cli
from evalcore.loader import save_suite


@pytest.fixture
def suite_path(suite, tmp_path: Path) -> Path:
    path = tmp_path / "suite.json"
    save_suite(suite, path)
    return path


def _run_with(monkeypatch, suite_path: Path, tmp_path: Path, argv: list[str]) -> dict:
    """Run `cmd_run` against a recording stand-in for LlamaServerModel."""
    captured: dict = {}

    class RecordingModel:
        ref = "llama-server:rec:Q4_K_M"

        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def generate(self, case) -> str:
            return "recorded [C1]"

    monkeypatch.setattr(cli, "LlamaServerModel", RecordingModel)
    out = tmp_path / "run.json"
    args = cli.build_parser().parse_args(
        ["run", "--suite", str(suite_path), "--out", str(out), *argv]
    )
    assert cli.cmd_run(args) == 0
    assert json.loads(out.read_text())["results"]
    return captured


def test_default_timeout_is_unchanged_at_180s(monkeypatch, suite_path, tmp_path) -> None:
    """The default must not move. Changing it would silently alter every existing
    caller, including the CI gate workflow."""
    captured = _run_with(
        monkeypatch, suite_path, tmp_path, ["--server-url", "http://127.0.0.1:8080"]
    )
    assert captured["timeout_s"] == 180.0


def test_timeout_flag_reaches_the_client(monkeypatch, suite_path, tmp_path) -> None:
    captured = _run_with(
        monkeypatch,
        suite_path,
        tmp_path,
        ["--server-url", "http://127.0.0.1:8080", "--timeout-s", "900"],
    )
    assert captured["timeout_s"] == 900.0
    assert captured["base_url"] == "http://127.0.0.1:8080"


def test_timeout_is_a_float_not_a_string(monkeypatch, suite_path, tmp_path) -> None:
    """`urlopen(timeout=...)` accepts a string without complaint and then compares
    it against a float at socket level, so a string here fails deep in stdlib
    partway through a two-hour run rather than at argument parsing."""
    captured = _run_with(
        monkeypatch,
        suite_path,
        tmp_path,
        ["--server-url", "http://127.0.0.1:8080", "--timeout-s", "42.5"],
    )
    assert isinstance(captured["timeout_s"], float)
    assert captured["timeout_s"] == 42.5
