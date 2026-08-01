"""Fetch docs repos into the gitignored scratch dir.

Two methods:

- ``git``     shallow clone with a blobless filter and sparse checkout. The default,
              and what a normal contributor runs.
- ``tarball`` codeload snapshot over plain HTTPS. No git binary needed, so it works
              in git-less CI and under agent sandboxes that deny git.

Both land the same tree at ``.scratch/<repo>/<docs_prefix>``.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from .. import config
from .repos import REPOS, RepoSpec


def target_dir(spec: RepoSpec) -> Path:
    return config.SCRATCH_DIR / spec.name


def fetch_git(spec: RepoSpec, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            spec.ref,
            "--filter=blob:none",
            "--sparse",
            spec.clone_url,
            str(dest),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "sparse-checkout", "set", spec.docs_prefix.rstrip("/")],
        check=True,
    )


def fetch_tarball(spec: RepoSpec, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(spec.tarball_url, timeout=300) as resp:  # noqa: S310
        raw = resp.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # codeload prefixes every path with "<repo>-<ref>/"
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if not rel.startswith(spec.docs_prefix) or not rel.endswith(".md"):
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is not None:
                out.write_bytes(extracted.read())


def fetch_all(method: str = "git", only: str | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for spec in REPOS:
        if only and spec.name != only:
            continue
        dest = target_dir(spec)
        print(f"fetching {spec.owner_repo}@{spec.ref} via {method} -> {dest}")
        if method == "git":
            fetch_git(spec, dest)
        elif method == "tarball":
            fetch_tarball(spec, dest)
        else:
            raise ValueError(f"unknown fetch method {method!r}")
        counts[spec.name] = sum(1 for _ in (dest / spec.docs_prefix).rglob("*.md"))
        print(f"  {counts[spec.name]} markdown files")
    return counts
