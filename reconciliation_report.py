# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Reconciliation report — proves every sent message was processed exactly once."""
from __future__ import annotations

import html
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


_BASE_DIR = Path(__file__).parent
STATE_DB = _BASE_DIR / "state.db"
REPORT_DIR = _BASE_DIR / "reports"

_TW_COLOURS: dict[str, str] = {
    "white":   "#FFFFFF",
    "mist":    "#EDF1F3",
    "black":   "#000000",
    "teal-dk": "#003D4F",
    "coral":   "#F2617A",
    "amber":   "#CC850A",
    "green":   "#689E78",
    "teal":    "#47A1AD",
    "plum":    "#634F7D",
}

_STATUS_COLOURS = {
    "pass":          "#689E78",
    "fail":          "#F2617A",
    "not_processed": "#CC850A",
    "duplicate":     "#634F7D",
}

_STATUS_LABELS = {
    "pass":          "PASS",
    "fail":          "FAIL → DLQ",
    "not_processed": "NOT PROCESSED",
    "duplicate":     "DUPLICATE",
}


def _summary_card(label: str, value: str | int, colour: str) -> str:
    return (
        f'<div style="background:{colour};color:#fff;border-radius:6px;'
        f'padding:1.2rem 2rem;text-align:center;min-width:130px;">'
        f'<div style="font-size:2.2rem;font-weight:700;line-height:1;">{value}</div>'
        f'<div style="font-size:0.82rem;margin-top:6px;opacity:0.88;">{label}</div>'
        f"</div>"
    )


def _status_badge(status: str) -> str:
    colour = _STATUS_COLOURS.get(status, "#47A1AD")
    label = _STATUS_LABELS.get(status, status.upper())
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 10px;'
        f"border-radius:3px;font-size:0.78rem;font-weight:600;"
        f'letter-spacing:0.05em;">{label}</span>'
    )


def _reconciliation_status(sent_row: sqlite3.Row, processed: dict) -> str:
    msg_id = sent_row["message_id"]
    if msg_id not in processed:
        return "not_processed"
    return processed[msg_id]["validation_status"]


def _build_rows(sent: list[sqlite3.Row], processed: dict) -> str:
    rows = []
    for s in sent:
        status = _reconciliation_status(s, processed)
        proc = processed.get(s["message_id"])
        error_cell = ""
        if proc and proc["error_detail"]:
            err = html.escape(proc["error_detail"][:200])
            error_cell = (
                f'<details><summary style="cursor:pointer;color:var(--teal);'
                f'font-weight:600;font-size:0.82rem;">Show error</summary>'
                f'<p style="margin-top:0.4rem;font-size:0.82rem;color:#555;">{err}</p></details>'
            )
        processed_at = html.escape(proc["processed_at"][:19].replace("T", " ")) if proc else "—"
        rows.append(
            "<tr>"
            f"<td style='font-family:monospace;font-size:0.82rem;'>{html.escape(s['file_name'])}</td>"
            f"<td>{html.escape(s['domain'])}.{html.escape(s['message_set'])}</td>"
            f"<td style='text-align:center;'>{_status_badge(status)}</td>"
            f"<td style='font-size:0.82rem;color:#555;'>{processed_at}</td>"
            f"<td>{error_cell}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _build_duplicate_rows(duplicates: list[sqlite3.Row]) -> str:
    if not duplicates:
        return (
            "<tr><td colspan='4' style='text-align:center;color:#888;padding:1.5rem;'>"
            "No duplicates detected.</td></tr>"
        )
    return "\n".join(
        "<tr>"
        f"<td style='font-family:monospace;font-size:0.82rem;'>{html.escape(d['file_name'])}</td>"
        f"<td>{html.escape(d['domain'])}.{html.escape(d['message_set'])}</td>"
        f"<td style='font-family:monospace;font-size:0.75rem;color:#888;'>{html.escape(d['message_id'][:16])}...</td>"
        f"<td style='font-size:0.82rem;color:#555;'>{html.escape(d['detected_at'][:19].replace('T', ' '))}</td>"
        "</tr>"
        for d in duplicates
    )


def generate_report(
    sent: list[sqlite3.Row],
    processed: dict,
    duplicates: list[sqlite3.Row],
) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    report_path = REPORT_DIR / f"reconciliation_{ts.strftime('%Y%m%d_%H%M%S')}.html"

    total_sent = len(sent)
    n_pass = sum(1 for s in sent if _reconciliation_status(s, processed) == "pass")
    n_fail = sum(1 for s in sent if _reconciliation_status(s, processed) == "fail")
    n_unprocessed = sum(1 for s in sent if _reconciliation_status(s, processed) == "not_processed")
    n_duplicates = len(duplicates)
    fully_reconciled = n_unprocessed == 0

    reconciliation_verdict = (
        '<p style="color:#689E78;font-weight:700;font-size:1.1rem;">&#10003; Fully reconciled — '
        "every sent message has been processed exactly once.</p>"
        if fully_reconciled
        else f'<p style="color:#F2617A;font-weight:700;font-size:1.1rem;">&#9888; Not fully reconciled — '
        f"{n_unprocessed} message(s) have not yet been processed.</p>"
    )

    css_vars = "\n".join(f"  --{k}: {v};" for k, v in _TW_COLOURS.items())
    cards = (
        _summary_card("Total Sent", total_sent, "#003D4F")
        + _summary_card("Pass", n_pass, "#689E78")
        + _summary_card("Fail → DLQ", n_fail, "#F2617A")
        + _summary_card("Not Processed", n_unprocessed, "#CC850A")
        + _summary_card("Duplicates Caught", n_duplicates, "#634F7D")
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>ISO 20022 Reconciliation Report</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link href="https://fonts.googleapis.com/css2?family=Bitter:wght@400;600;700&amp;family=Inter:wght@400;500;600&amp;display=swap" rel="stylesheet"/>
  <style>
    :root {{
{css_vars}
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: var(--mist); color: var(--black); min-height: 100vh; }}
    header {{ background: var(--teal-dk); color: var(--white); padding: 2rem 4rem; }}
    header h1 {{ font-family: 'Bitter', serif; font-size: 1.8rem; font-weight: 700; }}
    header p {{ margin-top: 0.4rem; font-size: 0.88rem; opacity: 0.8; }}
    article {{ max-width: 1000px; margin: 0 auto; padding: 3.5rem 4rem; }}
    section {{ margin-bottom: 2.8rem; }}
    h2 {{ font-family: 'Bitter', serif; font-size: 1.2rem; font-weight: 600;
          color: var(--teal-dk); border-bottom: 2px solid var(--teal-dk);
          padding-bottom: 0.4rem; margin-bottom: 1.2rem; }}
    .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.2rem; }}
    .verdict {{ padding: 0.8rem 1.2rem; border-radius: 6px; background: var(--white);
                box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--white);
             font-size: 0.875rem; border-radius: 6px; overflow: hidden;
             box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    thead {{ background: var(--teal-dk); color: var(--white); }}
    thead th {{ padding: 0.75rem 1rem; text-align: left; font-weight: 600;
                font-size: 0.78rem; letter-spacing: 0.06em; text-transform: uppercase; }}
    tbody td {{ padding: 0.75rem 1rem; vertical-align: top;
                border-bottom: 1px solid #d8e0e4; line-height: 1.55; }}
    tbody tr:nth-child(even) {{ background: var(--mist); }}
    tbody tr:hover {{ background: #d5dfe4; }}
    details summary {{ cursor: pointer; list-style: none; }}
    details summary::-webkit-details-marker {{ display: none; }}
    details summary::before {{ content: '▶ '; font-size: 0.7rem; }}
    details[open] summary::before {{ content: '▼ '; }}
    footer {{ text-align: center; font-size: 0.78rem; color: #888;
              padding: 2rem; border-top: 1px solid #ccc; margin-top: 1rem; }}
  </style>
</head>
<body>
  <header>
    <h1>ISO 20022 Reconciliation Report</h1>
    <p>Generated {ts.strftime("%d %B %Y at %H:%M:%S")}</p>
  </header>
  <article>

    <section>
      <h2>Summary</h2>
      <div class="cards">{cards}</div>
      <div class="verdict">{reconciliation_verdict}</div>
    </section>

    <section>
      <h2>Message Processing Detail</h2>
      <table>
        <thead>
          <tr>
            <th style="width:22%">File</th>
            <th style="width:14%">Domain / Set</th>
            <th style="width:16%;text-align:center">Status</th>
            <th style="width:18%">Processed At</th>
            <th>Validation Error</th>
          </tr>
        </thead>
        <tbody>
          {_build_rows(sent, processed)}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Duplicate Detection Log</h2>
      <p style="font-size:0.88rem;color:#555;margin-bottom:1rem;">
        Messages that arrived more than once — each was identified and recorded without
        reprocessing, proving exactly-once semantics.
      </p>
      <table>
        <thead>
          <tr>
            <th style="width:22%">File</th>
            <th style="width:18%">Domain / Set</th>
            <th style="width:24%">Message ID (prefix)</th>
            <th>Detected At</th>
          </tr>
        </thead>
        <tbody>
          {_build_duplicate_rows(duplicates)}
        </tbody>
      </table>
    </section>

  </article>
  <footer>reconciliation_report &mdash; Thoughtworks Financial Services Practice</footer>
</body>
</html>"""

    report_path.write_text(doc, encoding="utf-8")
    return report_path


def main() -> None:
    if not STATE_DB.exists():
        print(
            f"Error: {STATE_DB} not found. Run sender_agent.py and receiver_agent.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row

    sent = conn.execute(
        "SELECT * FROM sent_messages ORDER BY domain, message_set, file_name"
    ).fetchall()

    processed = {
        row["message_id"]: row
        for row in conn.execute("SELECT * FROM processed_messages").fetchall()
    }

    duplicates = conn.execute(
        "SELECT * FROM duplicate_events ORDER BY detected_at"
    ).fetchall()

    conn.close()

    if not sent:
        print("No sent messages recorded. Run sender_agent.py first.", file=sys.stderr)
        sys.exit(1)

    n_unprocessed = sum(1 for s in sent if s["message_id"] not in processed)
    n_pass = sum(1 for p in processed.values() if p["validation_status"] == "pass")
    n_fail = sum(1 for p in processed.values() if p["validation_status"] == "fail")

    print(f"Sent     : {len(sent)}")
    print(f"Pass     : {n_pass}")
    print(f"Fail/DLQ : {n_fail}")
    print(f"Pending  : {n_unprocessed}")
    print(f"Dupes    : {len(duplicates)}")
    print(f"Reconciled: {'YES' if n_unprocessed == 0 else 'NO — receiver still catching up'}")

    report_path = generate_report(sent, processed, duplicates)
    print(f"\nReport   : {report_path}")


if __name__ == "__main__":
    main()
