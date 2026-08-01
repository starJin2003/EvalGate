"""The four corpus repos.

All four are markdown-native docs for parts of the stack EvalGate itself runs on,
so the model learns to ground answers in documentation it will actually be asked about.

Airflow was in the original plan and was dropped: it carries 1,678 .rst against 192 .md,
and its markdown is governance files, agent skills, and dev tooling rather than user
docs. Grafana replaced it. See DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoSpec:
    name: str
    owner_repo: str
    ref: str
    docs_prefix: str
    url_base: str
    # Paths containing any of these fragments are skipped as non-documentation.
    exclude: tuple[str, ...] = ()

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner_repo}.git"

    @property
    def tarball_url(self) -> str:
        return f"https://codeload.github.com/{self.owner_repo}/tar.gz/refs/heads/{self.ref}"


REPOS: tuple[RepoSpec, ...] = (
    RepoSpec(
        name="fastapi",
        owner_repo="fastapi/fastapi",
        ref="master",
        # English only. The repo carries ~1,537 translated copies that would add
        # duplicate content in languages the model is not being trained on.
        docs_prefix="docs/en/docs/",
        url_base="https://fastapi.tiangolo.com/",
        # release-notes.md is a 20k-line changelog of dependency bumps and PR links.
        # It parses cleanly and is worthless as QA material.
        exclude=("docs/en/docs/release-notes.md",),
    ),
    RepoSpec(
        name="pydantic",
        owner_repo="pydantic/pydantic",
        ref="main",
        docs_prefix="docs/",
        url_base="https://docs.pydantic.dev/latest/",
        # docs/api/ is mkdocstrings stubs (`::: pydantic.BaseModel`) with no prose.
        # contributing and help pages are project process, not library documentation.
        exclude=("docs/api/", "docs/contributing.md", "docs/help_with_pydantic.md"),
    ),
    RepoSpec(
        name="prometheus",
        owner_repo="prometheus/docs",
        ref="main",
        docs_prefix="docs/",
        url_base="https://prometheus.io/docs/",
    ),
    RepoSpec(
        name="grafana",
        owner_repo="grafana/grafana",
        ref="main",
        docs_prefix="docs/sources/",
        url_base="https://grafana.com/docs/grafana/latest/",
        # whatsnew and release-notes are version announcements that generate
        # version-trivia questions rather than grounded documentation questions.
        exclude=(
            "docs/sources/developer-resources/api-reference/",
            "docs/sources/whatsnew/",
            "docs/sources/release-notes/",
        ),
    ),
)

REPOS_BY_NAME = {r.name: r for r in REPOS}

# Surface forms used to detect which project a sentence is talking about.
# Deliberately tight: "Alertmanager" is Prometheus-native but appears throughout
# Grafana's docs as an integration, so it would produce false attributions.
REPO_ALIASES: dict[str, tuple[str, ...]] = {
    "fastapi": ("fastapi",),
    "pydantic": ("pydantic",),
    "prometheus": ("prometheus", "promql"),
    "grafana": ("grafana",),
}
