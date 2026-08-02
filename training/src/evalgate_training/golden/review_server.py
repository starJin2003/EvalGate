"""Localhost UI for the golden hand review. One case per screen.

The layout is the whole point. Judging citation accuracy means reading a marker
in the answer and then reading the chunk it points at, 96 times over roughly 800
chunks; a stacked page turns every one of those into a scroll hunt. So the answer
and the retrieved chunks sit in two independently scrolling panes, every citation
marker is a link that scrolls its chunk into view in the other pane, and the
verdict form is pinned to the bottom and never scrolls away.

No CDN, no fonts, no network of any kind. Bound to 127.0.0.1 because it has no
auth and none of this needs to leave the machine.
"""

from __future__ import annotations

import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import config
from . import review
from .review import Case, Judgment

MARKER_RE = re.compile(r"\[(C\d+)\]")

_CSS = """
:root { color-scheme: light dark;
  --bg:#fff; --fg:#16181d; --muted:#6b7280; --line:#e2e5ea; --card:#f7f8fa;
  --accent:#2563eb; --pass:#15803d; --fail:#b91c1c; --chunkl:92%; --chipl:88%; --textl:32%; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e6e8ec; --muted:#9aa0a6; --line:#262b35; --card:#161a22;
          --accent:#60a5fa; --pass:#4ade80; --fail:#f87171;
          --chunkl:18%; --chipl:24%; --textl:75%; }
}
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--fg); overflow:hidden;
  font:14.5px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  display:grid; grid-template-rows:auto minmax(0,1fr) auto; }

header { display:flex; align-items:center; gap:.75rem; flex-wrap:wrap;
  padding:.6rem 1rem; border-bottom:1px solid var(--line); background:var(--card); }
.count { font-weight:700; font-size:.95rem; white-space:nowrap; }
.bar { flex:1; min-width:6rem; height:6px; border-radius:999px; background:var(--line);
  overflow:hidden; }
.bar > i { display:block; height:100%; background:var(--accent); }
.tag { font-size:.72rem; letter-spacing:.02em; border:1px solid var(--line);
  border-radius:999px; padding:.12rem .55rem; color:var(--muted); white-space:nowrap; }
.tag.done { color:var(--pass); border-color:var(--pass); }
.tag.refused { color:var(--fail); border-color:var(--fail); }
header a, footer a { color:var(--accent); text-decoration:none; font-size:.8rem; }
header a:hover, footer a:hover { text-decoration:underline; }

.panes { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  gap:1px; background:var(--line); min-height:0; }
.pane { background:var(--bg); overflow-y:auto; padding:1rem 1.15rem 2.5rem; min-width:0; }
.pane h2 { font-size:.7rem; text-transform:uppercase; letter-spacing:.09em;
  color:var(--muted); margin:0 0 .6rem; font-weight:600; display:flex; gap:.6rem;
  align-items:center; flex-wrap:wrap; }
.pane h2 button { background:transparent; color:var(--accent); border:1px solid var(--line);
  border-radius:999px; padding:.1rem .55rem; font:inherit; font-size:.68rem;
  text-transform:none; letter-spacing:0; }
@media (max-width:900px) { .panes { grid-template-columns:1fr; grid-auto-rows:minmax(0,1fr); } }

.q { font-size:1.02rem; font-weight:600; margin:0 0 1rem; }
.absent { font-size:.8rem; color:var(--muted); margin:-.5rem 0 1rem; }
.absent code { color:var(--fail); }
.answer { white-space:pre-wrap; word-wrap:break-word; border-left:3px solid var(--accent);
  padding:.1rem 0 .1rem .9rem; margin:0; }
.refusal-note { font-size:.78rem; color:var(--muted); margin:.9rem 0 0;
  border-top:1px dashed var(--line); padding-top:.6rem; }

a.cite { display:inline-block; font-size:.74rem; font-weight:700; text-decoration:none;
  border-radius:4px; padding:0 .3rem; margin:0 .06rem; vertical-align:baseline;
  background:hsl(var(--h) 70% var(--chipl)); color:hsl(var(--h) 80% var(--textl));
  border:1px solid hsl(var(--h) 55% 60% / .5); cursor:pointer; }
a.cite:hover { outline:2px solid hsl(var(--h) 70% 55%); }
a.cite.unknown { background:transparent; color:var(--fail); border-color:var(--fail); }

.chunk { border:1px solid var(--line); border-left:4px solid hsl(var(--h) 60% 55%);
  border-radius:6px; margin:0 0 .8rem; background:var(--card); scroll-margin-top:.5rem; }
.chunk > summary { display:flex; gap:.5rem; align-items:baseline; flex-wrap:wrap;
  cursor:pointer; padding:.5rem .7rem; font-size:.78rem; color:var(--muted); }
.chunk .lbl { font-weight:700; font-size:.74rem; border-radius:4px; padding:0 .32rem;
  background:hsl(var(--h) 70% var(--chipl)); color:hsl(var(--h) 80% var(--textl)); }
.chunk .path { color:var(--fg); font-weight:500; word-break:break-word; }
.chunk pre { margin:0; padding:.7rem .8rem; border-top:1px solid var(--line);
  white-space:pre-wrap; word-wrap:break-word; overflow-x:auto; font-size:.79rem;
  line-height:1.55; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.chunk.flash { background:hsl(var(--h) 70% var(--chunkl)); }

footer { border-top:1px solid var(--line); background:var(--card); padding:.6rem 1rem; }
form { display:flex; gap:1.1rem; align-items:center; flex-wrap:wrap; }
fieldset { border:0; margin:0; padding:0; display:flex; gap:.55rem; align-items:center;
  flex-wrap:wrap; }
legend { float:left; margin-right:.6rem; font-size:.7rem; text-transform:uppercase;
  letter-spacing:.09em; color:var(--muted); }
label.opt { display:inline-flex; align-items:center; gap:.28rem; font-size:.82rem;
  border:1px solid var(--line); border-radius:999px; padding:.16rem .6rem; cursor:pointer; }
label.opt:has(input:checked) { border-color:var(--accent); color:var(--accent); font-weight:600; }
label.opt.pass:has(input:checked) { border-color:var(--pass); color:var(--pass); }
label.opt.fail:has(input:checked) { border-color:var(--fail); color:var(--fail); }
label.opt kbd { font:inherit; font-size:.68rem; color:var(--muted); }
input[type=radio] { accent-color:var(--accent); margin:0; }
input[type=text] { flex:1; min-width:10rem; background:var(--bg); color:var(--fg);
  border:1px solid var(--line); border-radius:6px; padding:.32rem .6rem; font:inherit;
  font-size:.85rem; }
button { background:var(--accent); color:#fff; border:0; border-radius:6px;
  padding:.4rem 1.1rem; font:inherit; font-weight:600; cursor:pointer; }
.hint { font-size:.72rem; color:var(--muted); }

main.summary { overflow-y:auto; padding:1.5rem; }
main.summary pre { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:1rem; white-space:pre-wrap; font-size:.85rem; }
"""

_JS = """
const hues = {};
document.querySelectorAll('.chunk').forEach(c => hues[c.dataset.label] = c);

document.querySelectorAll('a.cite').forEach(a => {
  a.addEventListener('click', ev => {
    ev.preventDefault();
    const target = hues[a.dataset.label];
    if (!target) return;
    target.open = true;
    target.scrollIntoView({behavior:'smooth', block:'start'});
    target.classList.add('flash');
    setTimeout(() => target.classList.remove('flash'), 1200);
  });
});

const toggle = document.getElementById('toggle-all');
toggle.addEventListener('click', () => {
  const opening = toggle.textContent === 'expand all';
  document.querySelectorAll('.chunk').forEach(c => c.open = opening);
  toggle.textContent = opening ? 'collapse all' : 'expand all';
});

const form = document.getElementById('verdict');
const setVerdict = v => {
  const el = form.querySelector(`input[name=verdict][value="${v}"]`);
  if (el) el.checked = true;
  syncCriteria();
};
const syncCriteria = () => {
  const failing = form.querySelector('input[name=verdict][value=fail]').checked;
  form.querySelectorAll('input[name=criterion]').forEach(el => el.disabled = !failing);
};
form.addEventListener('change', ev => {
  if (ev.target.name === 'criterion') setVerdict('fail');
  if (ev.target.name === 'verdict') syncCriteria();
});
syncCriteria();

document.addEventListener('keydown', ev => {
  if (ev.target.tagName === 'INPUT' && ev.target.type === 'text') return;
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const go = sel => { const el = document.querySelector(sel); if (el) location.href = el.href; };
  if (ev.key === 'p') { setVerdict('pass'); form.submit(); }
  else if (['1','2','3','4'].includes(ev.key)) {
    const el = form.querySelector(`input[name=criterion][value="${ev.key}"]`);
    if (el) { setVerdict('fail'); el.checked = true; }
  }
  else if (ev.key === 'Enter') form.submit();
  else if (ev.key === 'ArrowLeft' || ev.key === '[') go('#prev');
  else if (ev.key === 'ArrowRight' || ev.key === ']') go('#next');
  else return;
  ev.preventDefault();
});
"""


def _hue(index: int, total: int) -> int:
    """Distinct hue per chunk, spread evenly so adjacent labels never collide."""
    return int((index * 360 / max(1, total) + 15) % 360)


def _answer_html(case: Case) -> str:
    """Escape first, then turn `[C3]` into a link. Markers contain nothing HTML
    special, so linkifying after escaping cannot reopen an injection."""
    hues = {c.label: _hue(i, len(case.context)) for i, c in enumerate(case.context)}

    def sub(match: re.Match[str]) -> str:
        label = match.group(1)
        if label not in hues:
            # A marker naming a chunk that was never supplied. Shown as-is and
            # flagged as unresolvable; whether that fails the case is the
            # reviewer's call under criterion 4, not this tool's.
            return f'<a class="cite unknown" data-label="{label}" href="#">{label}?</a>'
        return (
            f'<a class="cite" style="--h:{hues[label]}" data-label="{label}" '
            f'href="#chunk-{label}">{label}</a>'
        )

    return MARKER_RE.sub(sub, html.escape(case.answer))


def _chunks_html(case: Case) -> str:
    parts = []
    for i, chunk in enumerate(case.context):
        e = html.escape
        # Collapsed by default. Eight full chunks stacked open is ~4 screens of
        # scrolling per case; collapsed, all eight headers fit at once and a
        # citation click opens exactly the one being checked.
        parts.append(
            f'<details class="chunk" id="chunk-{chunk.label}" '
            f'data-label="{chunk.label}" style="--h:{_hue(i, len(case.context))}">'
            f'<summary><span class="lbl">{e(chunk.label)}</span>'
            f'<span class="tag">{e(chunk.repo)}</span>'
            f'<span class="path">{e(chunk.heading_path)}</span>'
            f'<a href="{e(chunk.source_url)}" target="_blank" rel="noreferrer">source</a>'
            f"</summary>"
            f"<pre>{e(chunk.content)}</pre>"
            f"</details>"
        )
    return "".join(parts)


def _verdict_form(case: Case, existing: Judgment | None, index: int) -> str:
    e = html.escape
    checked_pass = " checked" if existing and existing.verdict == "pass" else ""
    checked_fail = " checked" if existing and existing.verdict == "fail" else ""
    criteria = "".join(
        f'<label class="opt"><input type="radio" name="criterion" value="{n}"'
        f"{' checked' if existing and existing.failed_criterion == n else ''}>"
        f"<kbd>{n}</kbd> {e(name)}</label>"
        for n, name in sorted(config.REVIEW_CRITERIA.items())
    )
    note = e(existing.note) if existing else ""
    resubmit = (
        f'<span class="hint">judged {e(existing.at)} &mdash; submitting appends a '
        f"superseding record</span>"
        if existing
        else '<span class="hint">p pass &middot; 1-4 fail on that criterion &middot; '
        "enter save &middot; arrows navigate</span>"
    )
    return (
        f'<form id="verdict" method="post" action="/judge">'
        f'<input type="hidden" name="question_id" value="{e(case.question_id)}">'
        f'<input type="hidden" name="index" value="{index}">'
        f"<fieldset><legend>Verdict</legend>"
        f'<label class="opt pass"><input type="radio" name="verdict" value="pass"'
        f"{checked_pass}><kbd>p</kbd> pass</label>"
        f'<label class="opt fail"><input type="radio" name="verdict" value="fail"'
        f"{checked_fail}>fail</label></fieldset>"
        f"<fieldset><legend>First failed criterion</legend>{criteria}</fieldset>"
        f'<input type="text" name="note" placeholder="note (optional)" value="{note}">'
        f"<button type=submit>Save</button>{resubmit}</form>"
    )


def case_page(
    case: Case,
    index: int,
    total: int,
    existing: Judgment | None,
    judged_count: int,
) -> str:
    e = html.escape
    pct = round(100 * judged_count / total) if total else 0
    prev_link = (
        f'<a id="prev" href="/case/{index - 1}">&larr; prev</a>'
        if index > 1
        else '<span class="tag">start</span>'
    )
    next_link = (
        f'<a id="next" href="/case/{index + 1}">next &rarr;</a>'
        if index < total
        else '<span class="tag">end</span>'
    )
    refusal_badge = '<span class="tag refused">refused</span>' if case.refused else ""
    done_badge = (
        f'<span class="tag done">{e(existing.verdict)}'
        + (f" &middot; {e(existing.criterion_name or '')}" if existing.failed_criterion else "")
        + "</span>"
        if existing
        else ""
    )
    absent = (
        f'<p class="absent">Question was built around a symbol verified absent from the '
        f"corpus: <code>{e(case.absent_symbol)}</code></p>"
        if case.absent_symbol
        else ""
    )
    refusal_note = (
        '<p class="refusal-note">The teacher refused. The text above is verbatim.</p>'
        if case.refused
        else ""
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Golden review {index}/{total}</title><style>{_CSS}</style></head><body>"
        f'<header><span class="count">Case {index} of {total}</span>'
        f'<span class="bar"><i style="width:{pct}%"></i></span>'
        f'<span class="tag">{judged_count} judged</span>'
        f'<span class="tag">{e(case.category)}</span>'
        f'<span class="tag">{e(case.repo)}</span>'
        f'<span class="tag">{e(case.question_id)}</span>'
        f"{refusal_badge}{done_badge}"
        f'{prev_link}{next_link}<a href="/summary">summary</a></header>'
        '<div class="panes">'
        '<section class="pane"><h2>Question</h2>'
        f'<p class="q">{e(case.question)}</p>{absent}'
        f"<h2>Teacher answer</h2>"
        f'<div class="answer">{_answer_html(case)}</div>{refusal_note}</section>'
        f'<section class="pane"><h2>Retrieved context &mdash; {len(case.context)} chunks, '
        f"exactly what the teacher was given"
        f'<button type=button id="toggle-all">expand all</button></h2>'
        f"{_chunks_html(case)}</section>"
        "</div>"
        f"<footer>{_verdict_form(case, existing, index)}</footer>"
        f"<script>{_JS}</script></body></html>"
    )


def summary_page(summary: dict) -> str:
    e = html.escape
    complete = summary["unjudged"] == 0
    banner = (
        f"<p>All {summary['cases']} cases judged. Written to "
        f"<code>{e(str(config.GOLDEN_REVIEW_SUMMARY))}</code>.</p>"
        if complete
        else f"<p>{summary['unjudged']} case(s) still unjudged. "
        f'<a href="/">Resume at the first one</a>.</p>'
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Golden review summary</title><style>{_CSS}</style></head><body>"
        '<header><span class="count">Golden hand review</span>'
        '<a href="/">back to review</a></header>'
        f'<main class="summary">{banner}'
        f"<pre>{e(review.format_summary(summary))}</pre>"
        f"<pre>{e(json.dumps(summary, indent=2))}</pre></main></body></html>"
    )


# --- server -------------------------------------------------------------------
class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], cases: list[Case]) -> None:
        super().__init__(address, ReviewHandler)
        self.cases = cases
        self.by_id = {c.question_id: i for i, c in enumerate(cases, start=1)}
        self.judgments = review.load_judgments()
        self.lock = threading.Lock()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        pass

    # --- helpers
    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _resolve(self, token: str) -> int | None:
        """Accept either a 1-based index or a question id, so a case can be
        rejudged by the id that appears in the manifest and the log."""
        if token.isdigit():
            n = int(token)
            return n if 1 <= n <= len(self.server.cases) else None
        return self.server.by_id.get(token)

    def _next_unjudged(self, after: int) -> int | None:
        cases, judged = self.server.cases, self.server.judgments
        for i in range(after, len(cases) + 1):
            if cases[i - 1].question_id not in judged:
                return i
        return review.first_unjudged(cases, judged)

    # --- routes
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self._send("", status=404, ctype="text/plain")
            return
        if path == "/":
            nxt = review.first_unjudged(self.server.cases, self.server.judgments)
            self._redirect(f"/case/{nxt}" if nxt else "/summary")
            return
        if path == "/summary":
            summary = review.summarize(self.server.cases, self.server.judgments)
            if summary["unjudged"] == 0:
                review.write_summary(summary)
            self._send(summary_page(summary))
            return
        if path.startswith("/case/"):
            index = self._resolve(path[len("/case/") :])
            if index is None:
                self._send("no such case", status=404, ctype="text/plain")
                return
            case = self.server.cases[index - 1]
            self._send(
                case_page(
                    case,
                    index,
                    len(self.server.cases),
                    self.server.judgments.get(case.question_id),
                    len(self.server.judgments),
                )
            )
            return
        self._send("not found", status=404, ctype="text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/judge":
            self._send("not found", status=404, ctype="text/plain")
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        question_id = form.get("question_id", [""])[0]
        verdict = form.get("verdict", [""])[0]
        criterion = form.get("criterion", [""])[0]
        note = form.get("note", [""])[0]
        index = int(form.get("index", ["1"])[0])

        if question_id not in self.server.by_id:
            self._send("unknown case", status=400, ctype="text/plain")
            return
        try:
            with self.server.lock:
                judgment = review.record(
                    question_id,
                    verdict,
                    int(criterion) if criterion else None,
                    note,
                )
                self.server.judgments[question_id] = judgment
        except ValueError as exc:
            # Re-render the same case rather than losing the reviewer's typing.
            self._send(f"{html.escape(str(exc))} &mdash; <a href='/case/{index}'>back</a>", 400)
            return

        nxt = self._next_unjudged(index + 1)
        self._redirect(f"/case/{nxt}" if nxt else "/summary")


def serve(cases: list[Case], host: str, port: int) -> ReviewServer:
    return ReviewServer((host, port), cases)
