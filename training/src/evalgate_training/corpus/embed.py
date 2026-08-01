"""Embed chunks into pgvector."""

from __future__ import annotations

import json

from openai import OpenAI

from .. import config, db
from ..budget import Ledger
from .parse import Chunk

BATCH_SIZE = 128


def load_chunks(chunks: list[Chunk]) -> None:
    """Upsert chunk rows. Embeddings are filled in separately so a re-parse never
    silently discards work that was already paid for."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO chunks (chunk_id, repo, file_path, heading_path,
                                    source_url, content, content_sha256, token_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    repo = EXCLUDED.repo,
                    file_path = EXCLUDED.file_path,
                    heading_path = EXCLUDED.heading_path,
                    source_url = EXCLUDED.source_url,
                    content = EXCLUDED.content,
                    token_count = EXCLUDED.token_count,
                    embedding = CASE
                        WHEN chunks.content_sha256 IS DISTINCT FROM EXCLUDED.content_sha256
                        THEN NULL ELSE chunks.embedding END,
                    content_sha256 = EXCLUDED.content_sha256
                """,
                [
                    (
                        c.chunk_id,
                        c.repo,
                        c.file_path,
                        c.heading_path,
                        c.source_url,
                        c.content,
                        c.content_sha256,
                        c.token_count,
                    )
                    for c in chunks
                ],
            )
        conn.commit()


def write_manifest(chunks: list[Chunk]) -> None:
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with config.CHUNK_MANIFEST.open("w") as fh:
        for c in sorted(chunks, key=lambda c: (c.repo, c.file_path, c.chunk_id)):
            fh.write(json.dumps(c.manifest_row(), sort_keys=True) + "\n")


def embed_pending(ledger: Ledger, limit: int | None = None) -> dict[str, int]:
    """Embed every chunk with a NULL embedding. Cost is checked before each call."""
    client = OpenAI(api_key=config.openai_api_key())
    done = 0
    total_tokens = 0

    with db.connect() as conn:
        sql = "SELECT chunk_id, content, token_count FROM chunks WHERE embedding IS NULL"
        sql += " ORDER BY chunk_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        pending = conn.execute(sql).fetchall()

        if not pending:
            print("nothing to embed")
            return {"embedded": 0, "tokens": 0}

        projected = config.cost_usd(config.EMBED_MODEL, sum(r[2] for r in pending), 0, batch=False)
        ledger.reserve("corpus.embed", projected, sum(r[2] for r in pending))
        print(f"embedding {len(pending)} chunks, projected ${projected:.4f}")

        for start in range(0, len(pending), BATCH_SIZE):
            window = pending[start : start + BATCH_SIZE]
            resp = client.embeddings.create(model=config.EMBED_MODEL, input=[r[1] for r in window])
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE chunks SET embedding = %s::vector WHERE chunk_id = %s",
                    [
                        (str(item.embedding), row[0])
                        for item, row in zip(resp.data, window, strict=True)
                    ],
                )
            conn.commit()
            ledger.record(
                "corpus.embed", config.EMBED_MODEL, resp.usage.prompt_tokens, 0, batch=False
            )
            done += len(window)
            total_tokens += resp.usage.prompt_tokens
            print(f"  {done}/{len(pending)}  ${ledger.total_usd:.4f} spent")

    return {"embedded": done, "tokens": total_tokens}
