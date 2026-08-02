"""Diff reports: terminal, self-contained HTML, and a PR comment.

All three render from the same Diff, so the number in the PR comment and the
number on the report page cannot disagree.
"""

from __future__ import annotations

import html

from .diff import Diff

_RESET = "\033[0m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_DIM = "\033[2m"
_BOLD = "\033[1m"


def _sign(value: float) -> str:
    return f"{value:+.3f}"


def terminal_report(diff: Diff, color: bool = True, max_cases: int = 15) -> str:
    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    verdict = diff.verdict
    head = c(f" {verdict.upper()} ", _GREEN if verdict == "pass" else _RED)
    lines = [
        f"{head} {diff.suite_id}  {diff.baseline_run} -> {diff.candidate_run}",
        f"  overall {diff.baseline_score:.3f} -> {diff.candidate_score:.3f} ({_sign(diff.delta)})",
        "",
        c("  category         baseline  candidate     delta", _DIM),
    ]
    for name, s in diff.by_category().items():
        marker = _RED if s["delta"] < -1e-9 else (_GREEN if s["delta"] > 1e-9 else _DIM)
        lines.append(
            f"  {name:<16} {s['baseline']:8.3f}  {s['candidate']:9.3f}  "
            + c(f"{_sign(s['delta']):>8}", marker)
        )

    if diff.breaches:
        lines += ["", c("  Threshold breaches", _BOLD)]
        for b in diff.breaches:
            lines.append(c(f"  ! {b.scope}: {b.reason}", _RED))

    regressions = diff.regressions()
    if regressions:
        lines += ["", c(f"  Regressed cases ({len(regressions)})", _BOLD)]
        for d in regressions[:max_cases]:
            lines.append(
                f"  {c('-', _RED)} [{d.category}] {d.case_id}  "
                f"{d.baseline_score:.2f} -> {d.candidate_score:.2f}"
            )
            if d.candidate and d.candidate.scores:
                worst = min(d.candidate.scores, key=lambda s: s.score)
                lines.append(c(f"      {worst.kind}: {worst.rationale}", _DIM))
        if len(regressions) > max_cases:
            lines.append(c(f"      ... {len(regressions) - max_cases} more", _DIM))

    improvements = diff.improvements()
    if improvements:
        lines += ["", c(f"  Improved cases ({len(improvements)})", _DIM)]
    return "\n".join(lines)


def markdown_comment(diff: Diff, max_cases: int = 10) -> str:
    """The PR comment. Kept short enough to read without expanding."""
    icon = "✅" if diff.verdict == "pass" else "❌"
    out = [
        f"## {icon} eval-gate — `{diff.suite_id}`",
        "",
        f"**Overall {diff.baseline_score:.3f} → {diff.candidate_score:.3f} ({_sign(diff.delta)})**",
        "",
        "| Category | Baseline | Candidate | Delta |",
        "|---|---:|---:|---:|",
    ]
    for name, s in diff.by_category().items():
        flag = " ⚠️" if s["delta"] < -1e-9 else ""
        out.append(
            f"| {name} | {s['baseline']:.3f} | {s['candidate']:.3f} | {_sign(s['delta'])}{flag} |"
        )

    if diff.breaches:
        out += ["", "### Threshold breaches", ""]
        out += [f"- **{b.scope}** — {b.reason}" for b in diff.breaches]

    regressions = diff.regressions()
    if regressions:
        out += ["", f"<details><summary>Regressed cases ({len(regressions)})</summary>", ""]
        for d in regressions[:max_cases]:
            out.append(
                f"**`{d.case_id}`** ({d.category}) {d.baseline_score:.2f} → {d.candidate_score:.2f}"
            )
            if d.candidate:
                worst = min(d.candidate.scores, key=lambda s: s.score, default=None)
                if worst:
                    out.append(f"> {worst.kind}: {worst.rationale}")
                out.append("")
        if len(regressions) > max_cases:
            out.append(f"_...and {len(regressions) - max_cases} more._")
        out += ["", "</details>"]
    return "\n".join(out)


_CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --line:#ddd;
        --card:#fafafa; --bad:#b91c1c; --good:#15803d; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e6e6e6; --muted:#9aa0a6; --line:#2a2f3a;
          --card:#161a22; --bad:#f87171; --good:#4ade80; }
}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
     font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif}
main{max-width:62rem;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 .5rem}
.verdict{display:inline-block;padding:.15rem .6rem;border-radius:999px;
         font-weight:700;font-size:.8rem;color:#fff}
.pass{background:var(--good)} .fail{background:var(--bad)}
table{border-collapse:collapse;width:100%;margin:1rem 0}
th,td{border-bottom:1px solid var(--line);padding:.45rem .6rem;text-align:left}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:var(--bad)} .good{color:var(--good)}
.case{border:1px solid var(--line);border-radius:8px;background:var(--card);
      padding:.85rem 1rem;margin-bottom:.75rem}
.meta{font-size:.75rem;color:var(--muted);margin-bottom:.4rem}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media (max-width:48rem){.cols{grid-template-columns:1fr}}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);
    border-radius:6px;padding:.6rem;font-size:.8rem;overflow-x:auto;margin:.25rem 0}
.scroll{overflow-x:auto}
"""


def html_report(diff: Diff) -> str:
    e = html.escape
    v = diff.verdict
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>eval-gate {e(diff.suite_id)}</title><style>{_CSS}</style>",
        "</head><body><main>",
        f"<h1>eval-gate <span class='verdict {v}'>{v.upper()}</span></h1>",
        f"<p class='meta'>{e(diff.suite_id)} &middot; {e(diff.baseline_run)} "
        f"&rarr; {e(diff.candidate_run)}</p>",
        f"<p><strong>Overall {diff.baseline_score:.3f} &rarr; "
        f"{diff.candidate_score:.3f} ({_sign(diff.delta)})</strong></p>",
        "<div class='scroll'><table><thead><tr><th>Category</th>"
        "<th class='num'>Baseline</th><th class='num'>Candidate</th>"
        "<th class='num'>Delta</th></tr></thead><tbody>",
    ]
    for name, s in diff.by_category().items():
        cls = "bad" if s["delta"] < -1e-9 else ("good" if s["delta"] > 1e-9 else "")
        parts.append(
            f"<tr><td>{e(name)}</td><td class='num'>{s['baseline']:.3f}</td>"
            f"<td class='num'>{s['candidate']:.3f}</td>"
            f"<td class='num {cls}'>{_sign(s['delta'])}</td></tr>"
        )
    parts.append("</tbody></table></div>")

    if diff.breaches:
        parts.append("<h2>Threshold breaches</h2><ul>")
        parts += [
            f"<li class='bad'><strong>{e(b.scope)}</strong> — {e(b.reason)}</li>"
            for b in diff.breaches
        ]
        parts.append("</ul>")

    regressions = diff.regressions()
    if regressions:
        parts.append(f"<h2>Regressed cases ({len(regressions)})</h2>")
        for d in regressions:
            parts.append(
                f"<div class='case'><div class='meta'>{e(d.category)} &middot; "
                f"{e(d.case_id)} &middot; {d.baseline_score:.2f} &rarr; "
                f"{d.candidate_score:.2f}</div><div class='cols'>"
                f"<div><strong>Baseline</strong><pre>"
                f"{e(d.baseline.output if d.baseline else '(absent)')}</pre></div>"
                f"<div><strong>Candidate</strong><pre>"
                f"{e(d.candidate.output if d.candidate else '(absent)')}</pre></div>"
                f"</div>"
            )
            if d.candidate and d.candidate.scores:
                parts.append("<div class='meta'>")
                parts += [
                    f"{e(str(s.kind))} {s.score:.2f} — {e(s.rationale)}<br>"
                    for s in d.candidate.scores
                ]
                parts.append("</div>")
            parts.append("</div>")
    parts.append("</main></body></html>")
    return "".join(parts)
