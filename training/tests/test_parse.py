"""Parser tests. The fence-awareness and prose-filter cases are the ones that
actually protect corpus quality."""

from __future__ import annotations

from pathlib import Path

from evalgate_training.corpus import parse
from evalgate_training.corpus.repos import REPOS_BY_NAME

FASTAPI = REPOS_BY_NAME["fastapi"]
GRAFANA = REPOS_BY_NAME["grafana"]


def _write(tmp_path: Path, rel: str, text: str) -> tuple[Path, str]:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p, rel


def test_strip_frontmatter_returns_meta_and_body() -> None:
    meta, body = parse.strip_frontmatter("---\ntitle: Hello\nweight: 3\n---\n# H1\ntext\n")
    assert meta == {"title": "Hello", "weight": 3}
    assert body.startswith("# H1")


def test_frontmatter_absent_is_passthrough() -> None:
    meta, body = parse.strip_frontmatter("# H1\ntext\n")
    assert meta == {}
    assert body == "# H1\ntext\n"


def test_hash_inside_code_fence_is_not_a_heading(tmp_path: Path) -> None:
    prose = "Some explanatory prose about configuration that is long enough to survive. " * 4
    text = (
        "# Title\n\n"
        f"{prose}\n\n"
        "```python\n"
        "## This is a comment, not a heading\n"
        "x = 1\n"
        "```\n\n"
        f"{prose}\n"
    )
    abs_path, rel = _write(tmp_path, "docs/en/docs/a.md", text)
    chunks = parse.parse_file(FASTAPI, abs_path, rel)
    assert len(chunks) == 1
    assert "This is a comment" in chunks[0].content
    assert "comment" not in chunks[0].heading_path


def test_splits_on_h2_and_h3(tmp_path: Path) -> None:
    prose = "Documentation prose that is comfortably above the minimum length threshold. " * 4
    text = f"# Doc\n\n{prose}\n\n## Second\n\n{prose}\n\n### Third\n\n{prose}\n"
    abs_path, rel = _write(tmp_path, "docs/en/docs/b.md", text)
    chunks = parse.parse_file(FASTAPI, abs_path, rel)
    paths = [c.heading_path for c in chunks]
    assert paths == ["Doc", "Doc > Second", "Doc > Second > Third"]


def test_code_only_file_yields_no_chunks(tmp_path: Path) -> None:
    text = "# Example\n\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n"
    abs_path, rel = _write(tmp_path, "docs/en/docs/c.md", text)
    assert parse.parse_file(FASTAPI, abs_path, rel) == []


def test_anchor_and_source_url(tmp_path: Path) -> None:
    prose = "Enough prose here to clear the minimum threshold for a real chunk. " * 5
    text = f"# Doc\n\n{prose}\n\n## Using `Depends`\n\n{prose}\n"
    abs_path, rel = _write(tmp_path, "docs/en/docs/tutorial/first-steps.md", text)
    chunks = parse.parse_file(FASTAPI, abs_path, rel)
    assert chunks[0].source_url == "https://fastapi.tiangolo.com/tutorial/first-steps/"
    assert chunks[1].source_url.endswith("/tutorial/first-steps/#using-depends")


def test_index_files_drop_the_basename(tmp_path: Path) -> None:
    assert parse.file_url(GRAFANA, "docs/sources/alerting/_index.md").endswith("/alerting/")
    assert parse.file_url(FASTAPI, "docs/en/docs/index.md") == "https://fastapi.tiangolo.com/"


def test_hugo_shortcodes_are_stripped() -> None:
    assert "admonition" not in parse.clean_markdown("{{< admonition type=note >}}text")


def test_chunk_ids_are_stable_and_unique(tmp_path: Path) -> None:
    prose = "Stable identifier prose that exceeds the configured minimum length. " * 5
    text = f"# Doc\n\n{prose}\n\n## Two\n\n{prose}\n"
    abs_path, rel = _write(tmp_path, "docs/en/docs/d.md", text)
    first = parse.parse_file(FASTAPI, abs_path, rel)
    second = parse.parse_file(FASTAPI, abs_path, rel)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert len({c.chunk_id for c in first}) == len(first)


def test_manifest_row_excludes_chunk_text(tmp_path: Path) -> None:
    prose = "Manifest rows carry metadata only so docs are never vendored into git. " * 5
    abs_path, rel = _write(tmp_path, "docs/en/docs/e.md", f"# Doc\n\n{prose}\n")
    row = parse.parse_file(FASTAPI, abs_path, rel)[0].manifest_row()
    assert "content" not in row
    assert {"chunk_id", "repo", "file_path", "heading_path", "source_url"} <= row.keys()


def test_explicit_mkdocs_anchor_wins_over_the_slug(tmp_path: Path) -> None:
    """FastAPI pins ids as `## Title { #the-id }`. Slugifying the whole string
    produced a doubled, wrong fragment before this was handled."""
    prose = "Deployment prose long enough to clear the configured minimum length. " * 5
    text = f"# Doc\n\n{prose}\n\n## Previous Steps {{ #previous-steps }}\n\n{prose}\n"
    abs_path, rel = _write(tmp_path, "docs/en/docs/deployment/concepts.md", text)
    chunks = parse.parse_file(FASTAPI, abs_path, rel)
    assert chunks[1].heading_path == "Doc > Previous Steps"
    assert chunks[1].source_url.endswith("/deployment/concepts/#previous-steps")


def test_html_comments_are_stripped() -> None:
    assert "Leon" not in parse.clean_markdown("<!-- Leon Sorokin -->\ntext")


def test_split_heading_falls_back_to_slug() -> None:
    assert parse.split_heading("Using `Depends`") == ("Using `Depends`", "using-depends")
    assert parse.split_heading("Title { #pinned }") == ("Title", "pinned")
