"""Export the golden set for hand review, and the training split for P1.2.

The HTML page is self-contained and offline: no CDN, no fonts, no scripts beyond a
verdict tracker that writes to localStorage so a review can be done in sittings.
"""

from __future__ import annotations

import html
import json

from .. import config, db
from ..teacher.prompts import SYSTEM as TEACHER_SYSTEM

ROW_SQL = """
    SELECT q.question_id, q.category, q.repo, q.question, q.absent_symbol,
           q.retrieved, t.answer, t.refused, t.citations, t.valid, t.validation_errors
    FROM questions q
    JOIN teacher_answers t ON t.question_id = q.question_id
    WHERE q.split = %s
    ORDER BY q.category, q.repo, q.question_id
"""


def _rows(split: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(ROW_SQL, (split,)).fetchall()
        out = []
        for r in rows:
            chunk_rows = conn.execute(
                """SELECT chunk_id, repo, heading_path, source_url, content
                   FROM chunks WHERE chunk_id = ANY(%s)""",
                (r[5],),
            ).fetchall()
            by_id = {c[0]: c for c in chunk_rows}
            context = [
                {
                    "label": f"C{i}",
                    "chunk_id": cid,
                    "repo": by_id[cid][1],
                    "heading_path": by_id[cid][2],
                    "source_url": by_id[cid][3],
                    "content": by_id[cid][4],
                }
                for i, cid in enumerate(r[5], start=1)
                if cid in by_id
            ]
            out.append(
                {
                    "question_id": r[0],
                    "category": r[1],
                    "repo": r[2],
                    "question": r[3],
                    "absent_symbol": r[4],
                    "context": context,
                    "answer": r[6],
                    "refused": r[7],
                    "citations": r[8],
                    "valid": r[9],
                    "validation_errors": r[10],
                }
            )
        return out


def export_golden() -> dict[str, int]:
    rows = _rows("golden")
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with config.GOLDEN_JSONL.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    config.GOLDEN_HTML.write_text(_render_html(rows))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return counts


def export_training() -> dict[str, int]:
    """Chat-format rows for P1.2. Golden ids are excluded here and the exclusion is
    asserted, so an accidental leak fails loudly instead of contaminating eval."""
    rows = _rows("train")
    golden = set(json.loads(config.GOLDEN_IDS_FILE.read_text())["ids"])
    leaked = {r["question_id"] for r in rows} & golden
    if leaked:
        raise RuntimeError(f"{len(leaked)} golden ids leaked into the training split: {leaked}")

    kept = 0
    with config.DATASET_FILE.open("w") as fh:
        for row in rows:
            if not row["valid"]:
                continue
            context = "\n\n---\n\n".join(
                f"[{c['label']}] repo={c['repo']} | {c['heading_path']}\n"
                f"source: {c['source_url']}\n\n{c['content']}"
                for c in row["context"]
            )
            fh.write(
                json.dumps(
                    {
                        "question_id": row["question_id"],
                        "category": row["category"],
                        "repo": row["repo"],
                        "refused": row["refused"],
                        "messages": [
                            {"role": "system", "content": TEACHER_SYSTEM},
                            {
                                "role": "user",
                                "content": (
                                    f"Documentation excerpts:\n\n{context}\n\n"
                                    f"---\n\nQuestion: {row['question']}"
                                ),
                            },
                            {"role": "assistant", "content": row["answer"]},
                        ],
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            kept += 1
    return {"train_rows": kept, "skipped_invalid": len(rows) - kept, "golden_excluded": len(golden)}


# --- review page --------------------------------------------------------------
_CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --line:#ddd;
        --card:#fafafa; --warn:#b45309; --ok:#15803d; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e6e6e6; --muted:#9aa0a6; --line:#2a2f3a;
          --card:#161a22; --warn:#fbbf24; --ok:#4ade80; }
}
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1rem; background:var(--bg); color:var(--fg);
       font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif; }
main { max-width:60rem; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .25rem; }
.sub { color:var(--muted); margin:0 0 2rem; }
.case { border:1px solid var(--line); border-radius:8px; padding:1rem 1.25rem;
        margin-bottom:1.25rem; background:var(--card); }
.meta { display:flex; gap:.5rem; flex-wrap:wrap; font-size:.75rem;
        color:var(--muted); margin-bottom:.5rem; }
.tag { border:1px solid var(--line); border-radius:999px; padding:.1rem .55rem; }
.q { font-weight:600; margin:.25rem 0 .75rem; }
.answer { white-space:pre-wrap; border-left:3px solid var(--line); padding-left:.9rem;
          margin:.5rem 0; }
details { margin:.5rem 0; }
summary { cursor:pointer; color:var(--muted); font-size:.85rem; }
pre { overflow-x:auto; background:var(--bg); border:1px solid var(--line);
      border-radius:6px; padding:.75rem; font-size:.8rem; }
.chunk h4 { margin:.75rem 0 .25rem; font-size:.8rem; }
a { color:inherit; }
.verdict { display:flex; gap:.75rem; align-items:center; margin-top:.75rem;
           padding-top:.75rem; border-top:1px dashed var(--line); font-size:.85rem; }
.warn { color:var(--warn); }
.ok { color:var(--ok); }
#progress { position:sticky; top:0; background:var(--bg); padding:.75rem 0;
            border-bottom:1px solid var(--line); margin-bottom:1.5rem; font-size:.9rem; }
"""

_JS = """
const KEY='evalgate-golden-review';
const state=JSON.parse(localStorage.getItem(KEY)||'{}');
function paint(){
  const total=document.querySelectorAll('.case').length;
  const done=Object.keys(state).length;
  document.getElementById('progress').textContent=
    `Reviewed ${done} of ${total}. Verdicts persist in this browser; Export writes JSON.`;
}
document.addEventListener('change',e=>{
  if(e.target.name&&e.target.name.startsWith('v-')){
    state[e.target.name.slice(2)]=e.target.value;
    localStorage.setItem(KEY,JSON.stringify(state)); paint();
  }
});
document.addEventListener('DOMContentLoaded',()=>{
  for(const [id,v] of Object.entries(state)){
    const el=document.querySelector(`input[name="v-${id}"][value="${v}"]`);
    if(el) el.checked=true;
  }
  paint();
  document.getElementById('export').addEventListener('click',()=>{
    const blob=new Blob([JSON.stringify(state,null,2)],{type:'application/json'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download='golden_verdicts.json'; a.click();
  });
});
"""


def _render_html(rows: list[dict]) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>EvalGate golden set review</title>",
        f"<style>{_CSS}</style></head><body><main>",
        "<h1>Golden set review</h1>",
        "<p class='sub'>Teacher output is <strong>not</strong> ground truth until you "
        "accept it here. Check that the answer is supported by the cited excerpts and "
        "that refusals are genuinely unanswerable.</p>",
        "<div id='progress'></div>",
        "<p><button id='export'>Export verdicts JSON</button></p>",
    ]
    for row in rows:
        flags = ""
        if row["validation_errors"]:
            flags = f"<span class='tag warn'>{e(', '.join(row['validation_errors']))}</span>"
        parts.append(
            f"<section class='case' id='{e(row['question_id'])}'>"
            f"<div class='meta'>"
            f"<span class='tag'>{e(row['category'])}</span>"
            f"<span class='tag'>{e(row['repo'])}</span>"
            f"<span class='tag'>{e(row['question_id'])}</span>"
            f"<span class='tag {'warn' if row['refused'] else 'ok'}'>"
            f"{'refused' if row['refused'] else 'answered'}</span>"
            f"{flags}</div>"
            f"<div class='q'>{e(row['question'])}</div>"
        )
        if row["absent_symbol"]:
            parts.append(
                f"<div class='meta'>invented symbol: <code>{e(row['absent_symbol'])}</code></div>"
            )
        parts.append(f"<div class='answer'>{e(row['answer'])}</div>")
        parts.append(
            f"<details><summary>Retrieved context ({len(row['context'])} chunks)</summary>"
        )
        for c in row["context"]:
            parts.append(
                f"<div class='chunk'><h4>[{e(c['label'])}] {e(c['heading_path'])} "
                f"&mdash; <a href='{e(c['source_url'])}'>source</a></h4>"
                f"<pre>{e(c['content'][:2500])}</pre></div>"
            )
        parts.append("</details>")
        qid = e(row["question_id"])
        parts.append(
            f"<div class='verdict'><strong>Verdict:</strong>"
            f"<label><input type='radio' name='v-{qid}' value='accept'> accept</label>"
            f"<label><input type='radio' name='v-{qid}' value='edit'> needs edit</label>"
            f"<label><input type='radio' name='v-{qid}' value='reject'> reject</label>"
            f"</div></section>"
        )
    parts.append(f"</main><script>{_JS}</script></body></html>")
    return "".join(parts)
