"""Markdown to chunks.

Splits on H2 and H3. The scanner tracks fenced-code state so a ``#`` inside a code
block is never mistaken for a heading, which is the failure mode that quietly
shreds a docs corpus. Sections whose prose (code fences removed) falls below
``MIN_CHUNK_PROSE_CHARS`` are dropped, which is how "files that are only code
blocks" get skipped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .. import config
from .repos import RepoSpec

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Static-site-generator syntax that is noise to a reader and to a model.
HUGO_SHORTCODE_RE = re.compile(r"\{\{[<%].*?[%>]\}\}", re.DOTALL)  # Grafana
MKDOCS_INCLUDE_RE = re.compile(r"^\s*\{!.*?!\}\s*$", re.MULTILINE)  # FastAPI
MKDOCS_ADMONITION_RE = re.compile(r"^\s*///.*$", re.MULTILINE)  # FastAPI
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)  # editorial notes, Grafana bylines
INLINE_FMT_RE = re.compile(r"[`*_\[\]]")

# FastAPI and other mkdocs sites pin the anchor explicitly:
#   ## Previous Steps Before Starting { #previous-steps-before-starting }
# Slugifying that whole string produces a doubled, wrong anchor, so the declared
# id wins and is stripped from the visible heading text.
EXPLICIT_ANCHOR_RE = re.compile(r"\s*\{\s*#([A-Za-z0-9_.:-]+)\s*\}\s*$")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    repo: str
    file_path: str
    heading_path: str
    source_url: str
    content: str
    content_sha256: str
    token_count: int

    def manifest_row(self) -> dict[str, Any]:
        """Metadata only. Chunk text stays in Postgres and the dataset, so the
        repo never vendors documentation it does not own."""
        row = asdict(self)
        row.pop("content")
        return row


# --- token counting ----------------------------------------------------------
_ENCODER = None


def count_tokens(text: str) -> int:
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception:  # offline or download blocked
            _ENCODER = False
    if _ENCODER is False:
        return max(1, len(text) // 4)
    return len(_ENCODER.encode(text))


# --- helpers -----------------------------------------------------------------
def split_heading(raw: str) -> tuple[str, str]:
    """-> (visible text, anchor). An explicit `{ #id }` wins over the slug."""
    match = EXPLICIT_ANCHOR_RE.search(raw)
    if match:
        return EXPLICIT_ANCHOR_RE.sub("", raw).strip(), match.group(1)
    return raw.strip(), slugify(raw)


def slugify(heading: str) -> str:
    text = INLINE_FMT_RE.sub("", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text).strip("-")


def strip_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[match.end() :]


def clean_markdown(text: str) -> str:
    text = HTML_COMMENT_RE.sub("", text)
    text = HUGO_SHORTCODE_RE.sub("", text)
    text = MKDOCS_INCLUDE_RE.sub("", text)
    return MKDOCS_ADMONITION_RE.sub("", text)


def strip_code_fences(text: str) -> str:
    out, in_fence, fence = [], False, ""
    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if m and not in_fence:
            in_fence, fence = True, m.group(2)
            continue
        if m and in_fence and m.group(2)[0] == fence[0] and len(m.group(2)) >= len(fence):
            in_fence = False
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def prose_chars(text: str) -> int:
    prose = strip_code_fences(text)
    prose = re.sub(r"^\s*[|:>-].*$", "", prose, flags=re.MULTILINE)  # tables, quotes
    return len(re.sub(r"\s+", " ", prose).strip())


def file_url(spec: RepoSpec, rel_path: str) -> str:
    path = rel_path[len(spec.docs_prefix) :] if rel_path.startswith(spec.docs_prefix) else rel_path
    path = re.sub(r"\.md$", "", path)
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1].lower() in {"index", "_index", "readme"}:
        parts.pop()
    suffix = "/".join(parts)
    return spec.url_base + (f"{suffix}/" if suffix else "")


def split_oversized(content: str, limit: int) -> list[str]:
    """Split a too-long section at blank lines, never inside a code fence."""
    if count_tokens(content) <= limit:
        return [content]
    blocks, current, in_fence, fence = [], [], False, ""
    for line in content.splitlines():
        m = FENCE_RE.match(line)
        if m and not in_fence:
            in_fence, fence = True, m.group(2)
        elif m and in_fence and m.group(2)[0] == fence[0] and len(m.group(2)) >= len(fence):
            in_fence = False
        current.append(line)
        if not in_fence and not line.strip():
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))

    parts, buf = [], ""
    for block in blocks:
        candidate = f"{buf}\n{block}" if buf else block
        if buf and count_tokens(candidate) > limit:
            parts.append(buf)
            buf = block
        else:
            buf = candidate
    if buf.strip():
        parts.append(buf)
    return parts or [content]


# --- the parser ---------------------------------------------------------------
def parse_file(spec: RepoSpec, abs_path: Path, rel_path: str) -> list[Chunk]:
    raw = abs_path.read_text(encoding="utf-8", errors="replace")
    meta, body = strip_frontmatter(raw)
    body = clean_markdown(body)

    doc_title = str(meta.get("title") or meta.get("menuTitle") or "").strip()

    # Each level holds (visible text, anchor); the anchor of the deepest heading
    # becomes the source URL fragment.
    sections: list[tuple[list[tuple[str, str]], list[str]]] = []
    h1: tuple[str, str] = (doc_title, "")
    h2: tuple[str, str] = ("", "")
    h3: tuple[str, str] = ("", "")
    current: list[str] = []

    def heading_path() -> list[tuple[str, str]]:
        return [h for h in (h1, h2, h3) if h[0]]

    def flush() -> None:
        if current and "".join(current).strip():
            sections.append((heading_path(), list(current)))
        current.clear()

    in_fence, fence = False, ""
    for line in body.splitlines():
        m = FENCE_RE.match(line)
        if m and not in_fence:
            in_fence, fence = True, m.group(2)
            current.append(line)
            continue
        if m and in_fence and m.group(2)[0] == fence[0] and len(m.group(2)) >= len(fence):
            in_fence = False
            current.append(line)
            continue
        if in_fence:
            current.append(line)
            continue

        hm = HEADING_RE.match(line)
        if not hm:
            current.append(line)
            continue

        level = len(hm.group(1))
        heading = split_heading(hm.group(2).strip())
        if level == 1:
            flush()
            h1, h2, h3 = heading, ("", ""), ("", "")
        elif level == 2:
            flush()
            h2, h3 = heading, ("", "")
        elif level == 3:
            flush()
            h3 = heading
        else:
            current.append(line)
            continue
        current.append(line)
    flush()

    chunks: list[Chunk] = []
    for path_parts, lines in sections:
        content = "\n".join(lines).strip()
        if prose_chars(content) < config.MIN_CHUNK_PROSE_CHARS:
            continue
        anchor = path_parts[-1][1] if len(path_parts) > 1 else ""
        url = file_url(spec, rel_path) + (f"#{anchor}" if anchor else "")
        hpath = " > ".join(text for text, _ in path_parts) or rel_path

        for part_no, part in enumerate(split_oversized(content, config.MAX_CHUNK_TOKENS)):
            ordinal = len(chunks)
            digest = hashlib.sha256(
                f"{spec.name}|{rel_path}|{hpath}|{ordinal}".encode()
            ).hexdigest()[:16]
            chunks.append(
                Chunk(
                    chunk_id=digest,
                    repo=spec.name,
                    file_path=rel_path,
                    heading_path=hpath if part_no == 0 else f"{hpath} (part {part_no + 1})",
                    source_url=url,
                    content=part.strip(),
                    content_sha256=hashlib.sha256(part.strip().encode()).hexdigest(),
                    token_count=count_tokens(part),
                )
            )
    return chunks


def parse_repo(spec: RepoSpec, root: Path) -> list[Chunk]:
    docs_root = root / spec.docs_prefix
    if not docs_root.exists():
        raise FileNotFoundError(f"{docs_root} missing. Run `corpus fetch` first.")
    chunks: list[Chunk] = []
    for abs_path in sorted(docs_root.rglob("*.md")):
        rel_path = str(abs_path.relative_to(root))
        if any(frag in rel_path for frag in spec.exclude):
            continue
        chunks.extend(parse_file(spec, abs_path, rel_path))
    return chunks
