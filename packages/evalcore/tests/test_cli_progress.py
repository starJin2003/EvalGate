"""Incremental progress, and the pre-flight writability probe.

Both exist because of one incident: a CPU-only suite run on the OCI node did the
full inference and then died with EACCES on its single final write, discarding
every completed case. Two independent defences came out of it -- fail before the
work if the destination is unwritable, and persist each case as it lands so a
killed pod leaves evidence rather than nothing.
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


class _RecordingModel:
    ref = "llama-server:rec:Q4_K_M"

    def __init__(self, **kwargs) -> None:
        pass

    def generate(self, case) -> str:
        return "recorded [C1]"


def _args(suite_path: Path, out: Path, extra: list[str]):
    return cli.build_parser().parse_args(
        [
            "run",
            "--suite",
            str(suite_path),
            "--out",
            str(out),
            "--server-url",
            "http://127.0.0.1:8080",
            "--backend",
            "test",
            *extra,
        ]
    )


def test_progress_jsonl_has_one_line_per_case(monkeypatch, suite_path, tmp_path) -> None:
    monkeypatch.setattr(cli, "LlamaServerModel", _RecordingModel)
    out = tmp_path / "run.json"
    progress = tmp_path / "progress.jsonl"
    assert cli.cmd_run(_args(suite_path, out, ["--progress-jsonl", str(progress)])) == 0

    lines = [json.loads(line) for line in progress.read_text().splitlines() if line.strip()]
    final = json.loads(out.read_text())
    assert len(lines) == len(final["results"])
    assert [row["case_id"] for row in lines] == [r["case_id"] for r in final["results"]]


def test_progress_survives_a_crash_midway(monkeypatch, suite_path, tmp_path) -> None:
    """The whole point: a run that dies partway must still leave completed cases
    on disk. Before this, the artifact only existed after the last case."""

    class _DyingModel(_RecordingModel):
        calls = 0

        def generate(self, case) -> str:
            _DyingModel.calls += 1
            if _DyingModel.calls > 2:
                raise KeyboardInterrupt("pod killed")
            return "recorded [C1]"

    monkeypatch.setattr(cli, "LlamaServerModel", _DyingModel)
    out = tmp_path / "run.json"
    progress = tmp_path / "progress.jsonl"
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_run(_args(suite_path, out, ["--progress-jsonl", str(progress)]))

    assert not out.exists(), "the final artifact should not exist after a crash"
    lines = [line for line in progress.read_text().splitlines() if line.strip()]
    assert len(lines) == 2, "completed cases must be on disk despite the crash"


def test_unwritable_destination_fails_before_any_inference(
    monkeypatch, suite_path, tmp_path
) -> None:
    """Returns non-zero without calling the model even once. Two seconds of
    failure instead of two and a half hours."""
    calls: list[int] = []

    class _CountingModel(_RecordingModel):
        def generate(self, case) -> str:
            calls.append(1)
            return "recorded [C1]"

    monkeypatch.setattr(cli, "LlamaServerModel", _CountingModel)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        rc = cli.cmd_run(_args(suite_path, locked / "run.json", []))
        assert rc == 2
        assert calls == [], "no case should run when the output is unwritable"
    finally:
        locked.chmod(0o700)


def test_no_progress_file_is_written_when_the_flag_is_absent(
    monkeypatch, suite_path, tmp_path
) -> None:
    monkeypatch.setattr(cli, "LlamaServerModel", _RecordingModel)
    out = tmp_path / "run.json"
    assert cli.cmd_run(_args(suite_path, out, [])) == 0
    assert list(tmp_path.glob("*.jsonl")) == []
    # And the probe file must not be left behind.
    assert not list(tmp_path.glob(".*.writable"))
