"""
html_report.py — Render evaluation results as a standalone HTML page.

The terminal output is fine for development, but a reviewer (or a finance lead)
wants something readable. This writes a single self-contained .html file — no
external assets — summarising the run: headline accuracy, the regression-gate
verdict, latency, and a per-invoice table of decisions and reasons.

Design is deliberately a "control sheet" for an accounts-payable audit: monospace
figures, a tight ledger-style table, status pills for POST / REVIEW / REJECT.
"""

from __future__ import annotations

import datetime as _dt
import html as _html


def _pill(decision: str) -> str:
    colors = {
        "POST": ("#0f5132", "#d1e7dd"),
        "REVIEW": ("#664d03", "#fff3cd"),
        "REJECT": ("#842029", "#f8d7da"),
    }
    fg, bg = colors.get(decision, ("#41464b", "#e2e3e5"))
    return (
        f'<span class="pill" style="color:{fg};background:{bg}">'
        f"{_html.escape(decision)}</span>"
    )


def render_html_report(
    rows: list[dict],
    extraction_correct: int,
    extraction_total: int,
    decision_correct: int,
    decision_total: int,
    avg_latency: float,
    gate_passed: bool | None,
    mode: str,
    out_path: str,
) -> None:
    """rows: list of {source_file, vendor, extraction_str, decision, expected,
    ok, reasons}."""
    extraction_pct = 100 * extraction_correct / extraction_total
    decision_pct = 100 * decision_correct / decision_total
    generated = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if gate_passed is None:
        gate_html = '<span class="gate gate-na">no threshold set</span>'
    elif gate_passed:
        gate_html = '<span class="gate gate-pass">PASSED</span>'
    else:
        gate_html = '<span class="gate gate-fail">FAILED</span>'

    table_rows = ""
    for r in rows:
        reasons = "<br>".join(_html.escape(x) for x in r.get("reasons", [])) or "—"
        mark = "✓" if r["ok"] else "✗"
        mark_color = "#0f5132" if r["ok"] else "#842029"
        table_rows += f"""
        <tr>
          <td class="mono">{_html.escape(r['source_file'])}</td>
          <td>{_html.escape(r.get('vendor') or '—')}</td>
          <td class="mono center">{_html.escape(r['extraction_str'])}</td>
          <td>{_pill(r['decision'])}</td>
          <td>{_pill(r['expected'])}</td>
          <td class="center" style="color:{mark_color};font-weight:700">{mark}</td>
          <td class="reasons">{reasons}</td>
        </tr>"""

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoicing Agent — Evaluation Report</title>
<style>
  :root {{
    --ink:#1a1a1a; --muted:#6b6b6b; --line:#e3e0d8; --paper:#faf8f3;
    --accent:#1f3a5f;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--paper); color:var(--ink);
    font-family:Georgia, 'Times New Roman', serif; line-height:1.5;
  }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:48px 28px 80px; }}
  .eyebrow {{
    font-family:'Courier New', monospace; font-size:12px; letter-spacing:.18em;
    text-transform:uppercase; color:var(--muted);
  }}
  h1 {{ font-size:34px; margin:6px 0 4px; letter-spacing:-.01em; }}
  .sub {{ color:var(--muted); font-size:15px; margin-bottom:32px; }}
  .cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:14px; }}
  .card {{
    border:1px solid var(--line); background:#fff; padding:18px 20px;
  }}
  .card .label {{
    font-family:'Courier New', monospace; font-size:11px; letter-spacing:.12em;
    text-transform:uppercase; color:var(--muted); margin-bottom:8px;
  }}
  .card .figure {{ font-size:30px; font-weight:700; font-family:'Courier New',monospace; }}
  .card .detail {{ font-size:13px; color:var(--muted); margin-top:4px; }}
  .gatebar {{
    border:1px solid var(--line); background:#fff; padding:14px 20px;
    display:flex; align-items:center; justify-content:space-between; margin-bottom:34px;
    font-size:14px;
  }}
  .gate {{ font-family:'Courier New',monospace; font-weight:700; padding:3px 10px; font-size:13px; }}
  .gate-pass {{ color:#0f5132; background:#d1e7dd; }}
  .gate-fail {{ color:#842029; background:#f8d7da; }}
  .gate-na {{ color:#41464b; background:#e2e3e5; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); }}
  th {{
    text-align:left; font-family:'Courier New',monospace; font-size:11px;
    letter-spacing:.1em; text-transform:uppercase; color:var(--muted);
    padding:11px 12px; border-bottom:2px solid var(--line);
  }}
  td {{ padding:11px 12px; border-bottom:1px solid var(--line); font-size:14px; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  .mono {{ font-family:'Courier New',monospace; font-size:13px; }}
  .center {{ text-align:center; }}
  .reasons {{ font-size:13px; color:#444; max-width:320px; }}
  .pill {{
    font-family:'Courier New',monospace; font-size:11px; font-weight:700;
    padding:2px 9px; letter-spacing:.04em;
  }}
  .foot {{ margin-top:26px; font-size:12px; color:var(--muted); font-family:'Courier New',monospace; }}
  .rule {{ height:2px; background:var(--accent); width:54px; margin:18px 0 26px; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="eyebrow">Automated Invoicing Agent · Evaluation Control Sheet</div>
    <h1>Extraction &amp; Decision Report</h1>
    <div class="sub">Measured against a human-verified golden set · scoring mode: {_html.escape(mode)}</div>
    <div class="rule"></div>

    <div class="cards">
      <div class="card">
        <div class="label">Extraction accuracy</div>
        <div class="figure">{extraction_pct:.1f}%</div>
        <div class="detail">{extraction_correct}/{extraction_total} fields correct</div>
      </div>
      <div class="card">
        <div class="label">Decision accuracy</div>
        <div class="figure">{decision_pct:.1f}%</div>
        <div class="detail">{decision_correct}/{decision_total} invoices routed correctly</div>
      </div>
      <div class="card">
        <div class="label">Avg latency / invoice</div>
        <div class="figure">{avg_latency:.2f}s</div>
        <div class="detail">extraction call time</div>
      </div>
    </div>

    <div class="gatebar">
      <span>Regression gate</span>
      {gate_html}
    </div>

    <table>
      <thead>
        <tr>
          <th>Invoice</th><th>Vendor</th><th>Fields</th>
          <th>Decision</th><th>Expected</th><th>Match</th><th>Reasons / notes</th>
        </tr>
      </thead>
      <tbody>{table_rows}
      </tbody>
    </table>

    <div class="foot">Generated {generated} · Huma invoicing agent · golden-set evaluation</div>
  </div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
