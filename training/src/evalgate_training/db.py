"""Postgres schema and connection. Reuses the P0 pgvector dev stack."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from . import config

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    heading_path    TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    token_count     INTEGER NOT NULL,
    embedding       vector({dim})
);
CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repo);

CREATE TABLE IF NOT EXISTS questions (
    question_id     TEXT PRIMARY KEY,
    category        TEXT NOT NULL,
    repo            TEXT NOT NULL,
    question        TEXT NOT NULL,
    seed_chunk_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    absent_symbol   TEXT,
    split           TEXT NOT NULL DEFAULT 'train',
    retrieved       JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT questions_category_ck
        CHECK (category IN ('factual','howto','comparison','adversarial')),
    CONSTRAINT questions_split_ck CHECK (split IN ('train','golden'))
);
CREATE INDEX IF NOT EXISTS questions_category_idx ON questions (category);
CREATE INDEX IF NOT EXISTS questions_split_idx ON questions (split);

-- Regeneration attempt counter. Survives the delete-and-resubmit cycle that
-- `teacher audit --regenerate` uses, which is what makes the retry cap enforceable.
ALTER TABLE questions ADD COLUMN IF NOT EXISTS teacher_attempts INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS teacher_answers (
    question_id         TEXT PRIMARY KEY REFERENCES questions(question_id) ON DELETE CASCADE,
    model               TEXT NOT NULL,
    answer              TEXT NOT NULL,
    refused             BOOLEAN NOT NULL,
    citations           JSONB NOT NULL,
    valid               BOOLEAN NOT NULL,
    validation_errors   JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    batch_id            TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# HNSW is built separately: it needs the table to exist and is skipped when empty.
HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops)
"""


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(config.database_url()) as conn:
        register_vector(conn)
        yield conn


def init_schema() -> None:
    # No bind parameters: psycopg only allows a multi-statement script through the
    # simple query protocol, which parameters would switch off. EMBED_DIM is an int
    # constant from config, never user input.
    with connect() as conn:
        conn.execute(SCHEMA.format(dim=int(config.EMBED_DIM)))
        conn.commit()


def create_vector_index() -> None:
    with connect() as conn:
        conn.execute(HNSW_INDEX)
        conn.commit()


def counts() -> dict[str, Any]:
    with connect() as conn:
        out: dict[str, Any] = {}
        out["chunks"] = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        out["chunks_embedded"] = conn.execute(
            "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        out["questions"] = conn.execute("SELECT count(*) FROM questions").fetchone()[0]
        out["teacher_answers"] = conn.execute("SELECT count(*) FROM teacher_answers").fetchone()[0]
        out["by_repo"] = dict(
            conn.execute("SELECT repo, count(*) FROM chunks GROUP BY repo ORDER BY repo").fetchall()
        )
        out["by_category"] = dict(
            conn.execute(
                "SELECT category, count(*) FROM questions GROUP BY category ORDER BY category"
            ).fetchall()
        )
        return out
