# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "duckdb>=1.0,<2.0",
# ]
# ///
"""Reconciliation report — proves every sent message was processed exactly once."""
from __future__ import annotations

import html
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb


_BASE_DIR  = Path(__file__).parent
STATE_DB   = _BASE_DIR / "state.db"
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

_SCHEMA_COLOURS: list[str] = [
    "#003D4F", "#F2617A", "#CC850A", "#689E78",
    "#47A1AD", "#634F7D", "#E8882B", "#2D6DA4",
    "#B85C38", "#5B8C5A", "#9B59B6", "#1ABC9C",
]


@dataclass
class AnalyticsResult:
    schema_breakdown:  list[tuple] = field(default_factory=list)
    error_patterns:    list[tuple] = field(default_factory=list)
    category_outcomes: list[tuple] = field(default_factory=list)
    tampered_by_set:   list[tuple] = field(default_factory=list)
    latency_by_schema: list[tuple] = field(default_factory=list)
    pipeline_timeline: tuple | None = None
    latency_scatter:   list[tuple] = field(default_factory=list)


def _query_schema_breakdown(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("""
        SELECT
            s.message_set,
            COUNT(*)                                                              AS total_sent,
            COUNT(p.message_id)                                                   AS processed,
            SUM(CASE WHEN p.validation_status = 'pass' THEN 1 ELSE 0 END)        AS pass_count,
            SUM(CASE WHEN p.validation_status = 'fail' THEN 1 ELSE 0 END)        AS fail_count,
            COUNT(*) - COUNT(p.message_id)                                        AS pending,
            ROUND(
                CASE WHEN COUNT(p.message_id) > 0
                     THEN SUM(CASE WHEN p.validation_status = 'pass' THEN 1 ELSE 0 END)
                          * 100.0 / COUNT(p.message_id)
                     ELSE 0 END, 1
            )                                                                     AS pass_rate
        FROM state.sent_messages s
        LEFT JOIN state.processed_messages p ON s.message_id = p.message_id
        GROUP BY s.message_set
        ORDER BY s.message_set
    """).fetchall()


def _query_error_patterns(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("""
        SELECT
            CASE
                WHEN error_detail LIKE '%facet ''enumeration''%'  THEN 'Invalid enumeration value'
                WHEN error_detail LIKE '%facet ''pattern''%'       THEN 'Pattern / format violation'
                WHEN error_detail LIKE '%facet ''maxLength''%'     THEN 'String exceeds max length'
                WHEN error_detail LIKE '%facet ''minLength''%'     THEN 'Empty required field'
                WHEN error_detail LIKE '%facet ''minInclusive''%'  THEN 'Value below minimum'
                WHEN error_detail LIKE '%facet ''totalDigits''%'   THEN 'Too many digits'
                WHEN error_detail LIKE '%facet ''fractionDigits''%' THEN 'Too many decimal places'
                WHEN error_detail LIKE '%Missing child element%'   THEN 'Missing required element'
                WHEN error_detail LIKE '%not expected%'            THEN 'Wrong element order'
                WHEN error_detail LIKE '%XML syntax error%'        THEN 'XML syntax error'
                WHEN error_detail LIKE '%attribute ''Ccy''%'       THEN 'Missing currency (Ccy) attribute'
                ELSE 'Other validation error'
            END                              AS error_category,
            COUNT(*)                         AS occurrences,
            COUNT(DISTINCT message_set)      AS schemas_affected
        FROM state.processed_messages
        WHERE validation_status = 'fail'
          AND error_detail IS NOT NULL
        GROUP BY error_category
        ORDER BY occurrences DESC
    """).fetchall()


def _query_category_outcomes(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("""
        SELECT
            CASE
                WHEN file_name LIKE 'gen-pass-%' THEN 'gen-pass'
                WHEN file_name LIKE 'gen-fail-%' THEN 'gen-fail'
                WHEN file_name LIKE 'gen-edge-%' THEN 'gen-edge'
                ELSE 'hand-crafted'
            END              AS test_category,
            validation_status,
            COUNT(*)         AS count
        FROM state.processed_messages
        GROUP BY test_category, validation_status
        ORDER BY test_category, validation_status
    """).fetchall()


def _query_latency_by_schema(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("""
        SELECT
            s.message_set,
            COUNT(*)                                                                   AS msg_count,
            ROUND(MIN(
                epoch_ms(p.processed_at::TIMESTAMPTZ) - epoch_ms(s.sent_at::TIMESTAMPTZ)
            ) / 1000.0, 2)                                                             AS min_sec,
            ROUND(AVG(
                epoch_ms(p.processed_at::TIMESTAMPTZ) - epoch_ms(s.sent_at::TIMESTAMPTZ)
            ) / 1000.0, 2)                                                             AS avg_sec,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY
                epoch_ms(p.processed_at::TIMESTAMPTZ) - epoch_ms(s.sent_at::TIMESTAMPTZ)
            ) / 1000.0, 2)                                                             AS p95_sec,
            ROUND(MAX(
                epoch_ms(p.processed_at::TIMESTAMPTZ) - epoch_ms(s.sent_at::TIMESTAMPTZ)
            ) / 1000.0, 2)                                                             AS max_sec,
            ROUND(
                epoch_ms(MAX(p.processed_at::TIMESTAMPTZ)) - epoch_ms(MIN(p.processed_at::TIMESTAMPTZ))
            , 1)                                                                        AS receiver_span_ms
        FROM state.sent_messages s
        JOIN state.processed_messages p ON s.message_id = p.message_id
        GROUP BY s.message_set
        ORDER BY avg_sec DESC
    """).fetchall()


def _query_latency_scatter(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("""
        SELECT
            s.message_set,
            s.file_name,
            ROW_NUMBER() OVER (ORDER BY s.sent_at)                                    AS seq_num,
            ROUND((epoch_ms(p.processed_at::TIMESTAMPTZ)
                   - epoch_ms(s.sent_at::TIMESTAMPTZ)) / 1000.0, 3)                  AS latency_sec
        FROM state.sent_messages s
        JOIN state.processed_messages p ON s.message_id = p.message_id
        ORDER BY seq_num
    """).fetchall()


def _query_pipeline_timeline(conn: duckdb.DuckDBPyConnection) -> tuple | None:
    return conn.execute("""
        SELECT
            ROUND((epoch_ms(MAX(s.sent_at::TIMESTAMPTZ))
                   - epoch_ms(MIN(s.sent_at::TIMESTAMPTZ))) / 1000.0, 3)              AS sender_sec,
            ROUND((epoch_ms(MIN(p.processed_at::TIMESTAMPTZ))
                   - epoch_ms(MAX(s.sent_at::TIMESTAMPTZ))) / 1000.0, 3)              AS wait_sec,
            ROUND((epoch_ms(MAX(p.processed_at::TIMESTAMPTZ))
                   - epoch_ms(MIN(p.processed_at::TIMESTAMPTZ))) / 1000.0, 3)         AS receiver_sec,
            COUNT(*)                                                                   AS total_messages,
            ROUND(COUNT(*) * 1000.0 / NULLIF(
                epoch_ms(MAX(p.processed_at::TIMESTAMPTZ))
                - epoch_ms(MIN(p.processed_at::TIMESTAMPTZ)), 0
            ), 0)                                                                      AS throughput_per_sec
        FROM state.sent_messages s
        JOIN state.processed_messages p ON s.message_id = p.message_id
    """).fetchone()


def _query_tampered_by_set(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute("""
        SELECT
            domain || '.' || message_set AS schema_label,
            COUNT(*)                     AS tampered_count
        FROM state.tampered_messages
        GROUP BY schema_label
        ORDER BY tampered_count DESC
    """).fetchall()


def _run_analytics(db_path: Path) -> AnalyticsResult | None:
    try:
        conn = duckdb.connect()
        conn.execute(f"ATTACH '{db_path}' AS state (TYPE sqlite)")
        result = AnalyticsResult(
            schema_breakdown=_query_schema_breakdown(conn),
            error_patterns=_query_error_patterns(conn),
            category_outcomes=_query_category_outcomes(conn),
            tampered_by_set=_query_tampered_by_set(conn),
            latency_by_schema=_query_latency_by_schema(conn),
            pipeline_timeline=_query_pipeline_timeline(conn),
            latency_scatter=_query_latency_scatter(conn),
        )
        conn.close()
        return result
    except Exception as exc:
        print(f"Warning: DuckDB analytics failed — {exc}", file=sys.stderr)
        return None


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
    label  = _STATUS_LABELS.get(status, status.upper())
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


def _pass_rate_bar(rate: float) -> str:
    colour = "#689E78" if rate >= 80 else "#CC850A" if rate >= 50 else "#F2617A"
    return (
        f'<div style="display:flex;align-items:center;gap:0.5rem;">'
        f'<div style="flex:1;background:#e0eaee;border-radius:3px;height:8px;">'
        f'<div style="width:{rate}%;background:{colour};border-radius:3px;height:8px;"></div>'
        f'</div>'
        f'<span style="font-size:0.8rem;font-weight:600;color:{colour};min-width:42px;">{rate}%</span>'
        f'</div>'
    )


def _build_rows(sent: list[sqlite3.Row], processed: dict) -> str:
    rows = []
    for s in sent:
        status = _reconciliation_status(s, processed)
        proc   = processed.get(s["message_id"])
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


def _build_tampered_rows(tampered: list[sqlite3.Row]) -> str:
    if not tampered:
        return (
            "<tr><td colspan='5' style='text-align:center;color:#888;padding:1.5rem;'>"
            "No tampered messages detected — all signatures verified successfully.</td></tr>"
        )
    return "\n".join(
        "<tr>"
        f"<td style='font-family:monospace;font-size:0.82rem;'>{html.escape(t['file_name'])}</td>"
        f"<td>{html.escape(t['domain'])}.{html.escape(t['message_set'])}</td>"
        f"<td style='font-family:monospace;font-size:0.75rem;color:#888;'>{html.escape(t['message_id'][:16])}...</td>"
        f"<td style='font-size:0.82rem;color:#555;'>{html.escape(t['detected_at'][:19].replace('T', ' '))}</td>"
        f"<td style='font-size:0.82rem;color:#F2617A;'>{html.escape(t['tampered_topic'])}</td>"
        "</tr>"
        for t in tampered
    )


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


def _build_schema_breakdown_rows(rows: list[tuple]) -> str:
    if not rows:
        return "<tr><td colspan='7' style='text-align:center;color:#888;padding:1.5rem;'>No data.</td></tr>"
    out = []
    for msg_set, total, processed, pass_c, fail_c, pending, pass_rate in rows:
        pending_cell = (
            f'<span style="color:#CC850A;font-weight:600;">{pending}</span>'
            if pending > 0 else f'<span style="color:#689E78;">0</span>'
        )
        out.append(
            "<tr>"
            f"<td><strong>pain.{html.escape(str(msg_set))}</strong></td>"
            f"<td style='text-align:center;'>{total}</td>"
            f"<td style='text-align:center;'>{processed}</td>"
            f"<td style='text-align:center;color:#689E78;font-weight:600;'>{pass_c}</td>"
            f"<td style='text-align:center;color:#F2617A;font-weight:600;'>{fail_c}</td>"
            f"<td style='text-align:center;'>{pending_cell}</td>"
            f"<td style='min-width:140px;'>{_pass_rate_bar(float(pass_rate))}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _build_error_pattern_rows(rows: list[tuple]) -> str:
    if not rows:
        return "<tr><td colspan='3' style='text-align:center;color:#888;padding:1.5rem;'>No validation failures recorded.</td></tr>"
    max_count = rows[0][1] if rows else 1
    out = []
    for category, occurrences, schemas in rows:
        bar_width = int(occurrences / max_count * 100)
        out.append(
            "<tr>"
            f"<td>{html.escape(str(category))}</td>"
            f"<td style='min-width:160px;'>"
            f'<div style="display:flex;align-items:center;gap:0.5rem;">'
            f'<div style="flex:1;background:#e0eaee;border-radius:3px;height:8px;">'
            f'<div style="width:{bar_width}%;background:#F2617A;border-radius:3px;height:8px;"></div>'
            f'</div>'
            f'<span style="font-size:0.8rem;font-weight:600;color:#F2617A;min-width:24px;">{occurrences}</span>'
            f'</div></td>'
            f"<td style='text-align:center;color:#555;font-size:0.85rem;'>{schemas}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _build_category_outcome_rows(rows: list[tuple]) -> str:
    if not rows:
        return "<tr><td colspan='4' style='text-align:center;color:#888;padding:1.5rem;'>No data.</td></tr>"

    totals: dict[str, int] = {}
    for cat, _, count in rows:
        totals[cat] = totals.get(cat, 0) + count

    out = []
    prev_cat = None
    for cat, status, count in rows:
        cat_cell = ""
        if cat != prev_cat:
            cat_cell = f"<strong>{html.escape(str(cat))}</strong>"
            prev_cat = cat
        pct    = round(count / totals[cat] * 100, 1) if totals[cat] else 0
        colour = "#689E78" if status == "pass" else "#F2617A"
        note   = ""
        if (cat == "gen-pass" and status == "fail") or (cat == "gen-edge" and status == "fail"):
            note = ' <span style="color:#CC850A;font-size:0.75rem;">(unexpected — AI recategorised)</span>'
        if cat == "gen-fail" and status == "pass":
            note = ' <span style="color:#CC850A;font-size:0.75rem;">(unexpected — should have failed)</span>'
        out.append(
            "<tr>"
            f"<td style='font-family:monospace;font-size:0.85rem;'>{cat_cell}</td>"
            f"<td>{_status_badge(status)}{note}</td>"
            f"<td style='text-align:center;font-weight:600;color:{colour};'>{count}</td>"
            f"<td style='min-width:120px;'>{_pass_rate_bar(pct)}</td>"
            "</tr>"
        )
    return "\n".join(out)


def _build_tampered_by_set_rows(rows: list[tuple]) -> str:
    if not rows:
        return "<tr><td colspan='2' style='text-align:center;color:#888;padding:1.5rem;'>No tampered messages detected.</td></tr>"
    max_count = rows[0][1] if rows else 1
    out = []
    for schema_label, count in rows:
        bar_width = int(count / max_count * 100)
        out.append(
            "<tr>"
            f"<td><strong>{html.escape(str(schema_label))}</strong></td>"
            f"<td style='min-width:200px;'>"
            f'<div style="display:flex;align-items:center;gap:0.5rem;">'
            f'<div style="flex:1;background:#e0eaee;border-radius:3px;height:8px;">'
            f'<div style="width:{bar_width}%;background:#F2617A;border-radius:3px;height:8px;"></div>'
            f'</div>'
            f'<span style="font-size:0.8rem;font-weight:600;color:#F2617A;min-width:24px;">{count}</span>'
            f'</div></td>'
            "</tr>"
        )
    return "\n".join(out)


def _pipeline_phase_card(label: str, value: str, sub: str, colour: str) -> str:
    return (
        f'<div style="flex:1;background:{colour};color:#fff;border-radius:6px;'
        f'padding:1.1rem 1.4rem;min-width:160px;">'
        f'<div style="font-size:0.72rem;font-weight:600;letter-spacing:0.08em;'
        f'text-transform:uppercase;opacity:0.8;margin-bottom:0.3rem;">{label}</div>'
        f'<div style="font-size:1.7rem;font-weight:700;line-height:1;">{value}</div>'
        f'<div style="font-size:0.78rem;margin-top:0.4rem;opacity:0.85;">{sub}</div>'
        f'</div>'
    )


def _pipeline_phases_html(tl: tuple) -> str:
    sender_sec, wait_sec, receiver_sec, total_msgs, throughput = tl
    arrow = '<div style="font-size:1.4rem;color:#47A1AD;align-self:center;padding:0 0.3rem;">→</div>'
    phases = (
        _pipeline_phase_card("Sender publish", f"{sender_sec}s", f"{total_msgs} messages signed &amp; published", "#003D4F")
        + arrow
        + _pipeline_phase_card("Consumer startup", f"{wait_sec}s", "group rebalance across 12 topics", "#CC850A")
        + arrow
        + _pipeline_phase_card("Receiver burst", f"{receiver_sec}s", f"{int(throughput):,} msg/s · verify → validate → store", "#689E78")
    )
    return (
        f'<div style="display:flex;gap:0.6rem;flex-wrap:wrap;margin-bottom:1.4rem;">{phases}</div>'
    )


def _build_latency_rows(rows: list[tuple]) -> str:
    if not rows:
        return "<tr><td colspan='7' style='text-align:center;color:#888;padding:1.5rem;'>No latency data.</td></tr>"
    max_span = max(r[6] for r in rows) or 1
    out = []
    for msg_set, count, min_s, avg_s, p95_s, max_s, span_ms in rows:
        bar_pct = int(span_ms / max_span * 100)
        out.append(
            "<tr>"
            f"<td><strong>pain.{html.escape(str(msg_set))}</strong></td>"
            f"<td style='text-align:center;'>{count}</td>"
            f"<td style='text-align:center;font-family:monospace;'>{min_s}</td>"
            f"<td style='text-align:center;font-family:monospace;font-weight:600;'>{avg_s}</td>"
            f"<td style='text-align:center;font-family:monospace;'>{p95_s}</td>"
            f"<td style='text-align:center;font-family:monospace;'>{max_s}</td>"
            f"<td style='min-width:140px;'>"
            f'<div style="display:flex;align-items:center;gap:0.5rem;">'
            f'<div style="flex:1;background:#e0eaee;border-radius:3px;height:8px;">'
            f'<div style="width:{bar_pct}%;background:#47A1AD;border-radius:3px;height:8px;"></div>'
            f'</div>'
            f'<span style="font-size:0.78rem;color:#555;min-width:48px;">{span_ms:.0f} ms</span>'
            f'</div>'
            "</td>"
            "</tr>"
        )
    return "\n".join(out)


_DUCKDB_BADGE = '<span style="font-size:0.72rem;font-weight:400;color:#47A1AD;margin-left:0.5rem;">powered by DuckDB</span>'


def _schema_breakdown_html(rows: list[tuple]) -> str:
    return f"""
    <section>
      <h2>Analytics — Schema Breakdown {_DUCKDB_BADGE}</h2>
      <p style="font-size:0.87rem;color:#555;margin-bottom:1rem;">
        Pass rate and processing status grouped by ISO 20022 message set.
      </p>
      <table>
        <thead>
          <tr>
            <th>Message Set</th>
            <th style="text-align:center">Sent</th>
            <th style="text-align:center">Processed</th>
            <th style="text-align:center">Pass</th>
            <th style="text-align:center">Fail</th>
            <th style="text-align:center">Pending</th>
            <th style="min-width:160px;">Pass Rate</th>
          </tr>
        </thead>
        <tbody>
          {_build_schema_breakdown_rows(rows)}
        </tbody>
      </table>
    </section>"""


def _error_patterns_html(rows: list[tuple]) -> str:
    return f"""
    <section>
      <h2>Analytics — Validation Error Patterns {_DUCKDB_BADGE}</h2>
      <p style="font-size:0.87rem;color:#555;margin-bottom:1rem;">
        Most common categories of XSD validation failure across all processed messages.
      </p>
      <table>
        <thead>
          <tr>
            <th>Error Category</th>
            <th>Occurrences</th>
            <th style="text-align:center">Schemas Affected</th>
          </tr>
        </thead>
        <tbody>
          {_build_error_pattern_rows(rows)}
        </tbody>
      </table>
    </section>"""


def _category_outcomes_html(rows: list[tuple]) -> str:
    return f"""
    <section>
      <h2>Analytics — Test Category vs Actual Outcome {_DUCKDB_BADGE}</h2>
      <p style="font-size:0.87rem;color:#555;margin-bottom:1rem;">
        How AI-generated test files performed against the XSD — surfacing any mismatches
        between the intended category and the actual validation result.
      </p>
      <table>
        <thead>
          <tr>
            <th style="width:18%">Test Category</th>
            <th style="width:28%">Validation Outcome</th>
            <th style="width:12%;text-align:center">Count</th>
            <th>Share within category</th>
          </tr>
        </thead>
        <tbody>
          {_build_category_outcome_rows(rows)}
        </tbody>
      </table>
    </section>"""


def _tampered_by_set_html(rows: list[tuple]) -> str:
    if not rows:
        return ""
    return f"""
    <section>
      <h2>Analytics — Tampered Messages by Schema {_DUCKDB_BADGE}</h2>
      <p style="font-size:0.87rem;color:#555;margin-bottom:1rem;">
        Messages whose Ed25519 signature failed verification, grouped by ISO 20022 message set.
        These were forwarded to <code>iso20022.tampered</code> and excluded from XSD validation.
      </p>
      <table>
        <thead>
          <tr>
            <th>Schema</th>
            <th>Tampered Count</th>
          </tr>
        </thead>
        <tbody>
          {_build_tampered_by_set_rows(rows)}
        </tbody>
      </table>
    </section>"""


def _build_scatter_svg(rows: list[tuple]) -> str:
    if not rows:
        return '<p style="color:#888;">No scatter data available.</p>'

    W, H = 820, 410
    ml, mr, mt, mb = 68, 175, 30, 55
    pw, ph = W - ml - mr, H - mt - mb

    schemas: list[str] = list(dict.fromkeys(r[0] for r in rows))
    colour_map = {s: _SCHEMA_COLOURS[i % len(_SCHEMA_COLOURS)] for i, s in enumerate(schemas)}

    lat_vals = [r[3] for r in rows]
    seq_max  = len(rows)
    y_lo_raw, y_hi_raw = min(lat_vals), max(lat_vals)
    y_pad    = max((y_hi_raw - y_lo_raw) * 0.12, 0.05)
    y_lo, y_hi = y_lo_raw - y_pad, y_hi_raw + y_pad

    def sx(seq: int) -> float:
        return ml + (seq - 1) / max(seq_max - 1, 1) * pw

    def sy(lat: float) -> float:
        return mt + ph - (lat - y_lo) / (y_hi - y_lo) * ph

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'style="width:100%;max-width:{W}px;font-family:Inter,sans-serif;display:block;">'
    ]

    parts.append(f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="#f5f8fa" rx="2"/>')

    n_y = 5
    for i in range(n_y + 1):
        lat = y_lo + (y_hi - y_lo) * i / n_y
        yp  = sy(lat)
        parts.append(f'<line x1="{ml}" y1="{yp:.1f}" x2="{ml+pw}" y2="{yp:.1f}" stroke="#dde3e7" stroke-width="1"/>')
        parts.append(
            f'<text x="{ml-7}" y="{yp:.1f}" text-anchor="end" dominant-baseline="middle" '
            f'font-size="11" fill="#555">{lat:.2f}</text>'
        )

    tick_step = 100
    for seq in list(range(1, seq_max, tick_step)) + [seq_max]:
        xp = sx(seq)
        parts.append(f'<line x1="{xp:.1f}" y1="{mt+ph}" x2="{xp:.1f}" y2="{mt+ph+4}" stroke="#888" stroke-width="1"/>')
        parts.append(
            f'<text x="{xp:.1f}" y="{mt+ph+16}" text-anchor="middle" font-size="11" fill="#555">{seq}</text>'
        )

    parts.append(f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ph}" stroke="#003D4F" stroke-width="1.5"/>')
    parts.append(f'<line x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}" stroke="#003D4F" stroke-width="1.5"/>')

    cx = ml + pw // 2
    parts.append(
        f'<text x="{cx}" y="{H-7}" text-anchor="middle" font-size="12" fill="#003D4F" font-weight="600">'
        f'Message Sequence (send order)</text>'
    )
    ry = mt + ph // 2
    parts.append(
        f'<text x="13" y="{ry}" text-anchor="middle" dominant-baseline="middle" '
        f'font-size="12" fill="#003D4F" font-weight="600" transform="rotate(-90,13,{ry})">'
        f'Latency (s)</text>'
    )

    for schema in schemas:
        colour = colour_map[schema]
        for msg_set, file_name, seq, lat in rows:
            if msg_set != schema:
                continue
            xp, yp = sx(seq), sy(lat)
            parts.append(
                f'<circle cx="{xp:.1f}" cy="{yp:.1f}" r="3.5" '
                f'fill="{colour}" fill-opacity="0.72" stroke="{colour}" stroke-width="0.4">'
                f'<title>pain.{html.escape(msg_set)} | {html.escape(file_name)} | {lat}s</title>'
                f'</circle>'
            )

    lx = W - mr + 14
    for i, schema in enumerate(schemas):
        colour = colour_map[schema]
        ly = mt + i * 26
        parts.append(f'<circle cx="{lx+6}" cy="{ly+9}" r="5.5" fill="{colour}" fill-opacity="0.85"/>')
        parts.append(
            f'<text x="{lx+18}" y="{ly+9}" dominant-baseline="middle" font-size="12" fill="#333">'
            f'pain.{html.escape(schema)}</text>'
        )

    parts.append('</svg>')
    return "\n".join(parts)


def _latency_scatter_html(rows: list[tuple]) -> str:
    return f"""
    <section>
      <h2>Analytics — Latency Scatter Plot {_DUCKDB_BADGE}</h2>
      <p style="font-size:0.87rem;color:#555;margin-bottom:1.2rem;">
        Each point is one message. X-axis: send-order sequence (1–{len(rows)}).
        Y-axis: end-to-end latency in seconds. Colour: ISO 20022 message set.
        Hover a point to see file name and exact latency.
        Messages within the same schema cluster together on the x-axis (sent in rapid succession);
        the slight left-to-right decline reflects the sender's publish window — later schemas
        entered Kafka closer to the receiver's burst window.
      </p>
      {_build_scatter_svg(rows)}
    </section>"""


def _latency_html(rows: list[tuple], timeline: tuple | None) -> str:
    phases_html = _pipeline_phases_html(timeline) if timeline else ""
    return f"""
    <section>
      <h2>Analytics — Message Processing Latency {_DUCKDB_BADGE}</h2>
      <p style="font-size:0.87rem;color:#555;margin-bottom:1rem;">
        End-to-end latency from <code>sent_at</code> to <code>processed_at</code> per message.
        The pipeline has three distinct phases: sender publish, consumer group startup, and receiver burst.
        The <em>Receiver burst span</em> bar shows actual processing time per schema within the burst window.
      </p>
      {phases_html}
      <table>
        <thead>
          <tr>
            <th>Message Set</th>
            <th style="text-align:center">Count</th>
            <th style="text-align:center">Min (s)</th>
            <th style="text-align:center">Avg (s)</th>
            <th style="text-align:center">p95 (s)</th>
            <th style="text-align:center">Max (s)</th>
            <th style="min-width:180px;">Receiver Burst Span</th>
          </tr>
        </thead>
        <tbody>
          {_build_latency_rows(rows)}
        </tbody>
      </table>
    </section>"""


def _analytics_html(analytics: AnalyticsResult | None) -> str:
    if analytics is None:
        return ""
    return (
        _schema_breakdown_html(analytics.schema_breakdown)
        + _error_patterns_html(analytics.error_patterns)
        + _category_outcomes_html(analytics.category_outcomes)
        + _tampered_by_set_html(analytics.tampered_by_set)
        + _latency_html(analytics.latency_by_schema, analytics.pipeline_timeline)
        + _latency_scatter_html(analytics.latency_scatter)
    )


def generate_report(
    sent: list[sqlite3.Row],
    processed: dict,
    duplicates: list[sqlite3.Row],
    tampered: list[sqlite3.Row],
    analytics: AnalyticsResult | None = None,
) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    ts          = datetime.now()
    report_path = REPORT_DIR / f"reconciliation_{ts.strftime('%Y%m%d_%H%M%S')}.html"

    total_sent    = len(sent)
    n_pass        = sum(1 for s in sent if _reconciliation_status(s, processed) == "pass")
    n_fail        = sum(1 for s in sent if _reconciliation_status(s, processed) == "fail")
    n_unprocessed = sum(1 for s in sent if _reconciliation_status(s, processed) == "not_processed")
    n_duplicates  = len(duplicates)
    n_tampered    = len(tampered)

    reconciliation_verdict = (
        '<p style="color:#689E78;font-weight:700;font-size:1.1rem;">&#10003; Fully reconciled — '
        "every sent message has been processed exactly once.</p>"
        if n_unprocessed == 0
        else f'<p style="color:#F2617A;font-weight:700;font-size:1.1rem;">&#9888; Not fully reconciled — '
        f"{n_unprocessed} message(s) have not yet been processed.</p>"
    )

    css_vars = "\n".join(f"  --{k}: {v};" for k, v in _TW_COLOURS.items())
    cards = (
        _summary_card("Total Sent",        total_sent,   "#003D4F")
        + _summary_card("Pass",            n_pass,       "#689E78")
        + _summary_card("Fail → DLQ",      n_fail,       "#F2617A")
        + _summary_card("Not Processed",   n_unprocessed, "#CC850A")
        + _summary_card("Duplicates",      n_duplicates,  "#634F7D")
        + _summary_card("Tampered",        n_tampered,    "#F2617A")
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
    p {{ font-size: 0.9rem; color: #333; margin-bottom: 0.6rem; }}
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

    {_analytics_html(analytics)}

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
      <p>
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

    <section>
      <h2>Tampered Message Log</h2>
      <p>
        Messages whose Ed25519 digital signature failed verification. These were
        forwarded to <code>iso20022.tampered</code> and never submitted for XSD validation.
      </p>
      <table>
        <thead>
          <tr>
            <th style="width:22%">File</th>
            <th style="width:14%">Domain / Set</th>
            <th style="width:22%">Message ID (prefix)</th>
            <th style="width:16%">Detected At</th>
            <th>Tampered Topic</th>
          </tr>
        </thead>
        <tbody>
          {_build_tampered_rows(tampered)}
        </tbody>
      </table>
    </section>

  </article>
  <footer>reconciliation_report &mdash; DuckDB analytics &mdash; Thoughtworks Financial Services Practice</footer>
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

    tampered = conn.execute(
        "SELECT * FROM tampered_messages ORDER BY detected_at"
    ).fetchall()

    conn.close()

    if not sent:
        print("No sent messages recorded. Run sender_agent.py first.", file=sys.stderr)
        sys.exit(1)

    n_unprocessed = sum(1 for s in sent if s["message_id"] not in processed)
    n_pass        = sum(1 for p in processed.values() if p["validation_status"] == "pass")
    n_fail        = sum(1 for p in processed.values() if p["validation_status"] == "fail")

    print(f"Sent     : {len(sent)}")
    print(f"Pass     : {n_pass}")
    print(f"Fail/DLQ : {n_fail}")
    print(f"Pending  : {n_unprocessed}")
    print(f"Dupes    : {len(duplicates)}")
    print(f"Tampered : {len(tampered)}")
    print(f"Reconciled: {'YES' if n_unprocessed == 0 else 'NO — receiver still catching up'}")

    print("Running DuckDB analytics ...")
    analytics = _run_analytics(STATE_DB)
    if analytics:
        print(f"  Schema breakdown  : {len(analytics.schema_breakdown)} message sets")
        print(f"  Error patterns    : {len(analytics.error_patterns)} categories")
        print(f"  Category outcomes : {len(analytics.category_outcomes)} rows")
        if analytics.pipeline_timeline:
            sender_sec, wait_sec, receiver_sec, _, throughput = analytics.pipeline_timeline
            print(f"  Pipeline latency  : sender {sender_sec}s | wait {wait_sec}s | receiver {receiver_sec}s | {int(throughput):,} msg/s")

    report_path = generate_report(sent, processed, duplicates, tampered, analytics)
    print(f"\nReport   : {report_path}")


if __name__ == "__main__":
    main()
