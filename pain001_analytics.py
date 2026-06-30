# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.0,<2.0", "lxml>=5.0", "pandas>=2.0"]
# ///
"""DuckDB analytics over pain.001 XML test messages + pipeline state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from lxml import etree

_NS       = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.13"
_NS_MAP   = {"p": _NS}
_BASE_DIR = Path(__file__).parent
_XML_DIR  = _BASE_DIR / "test_data" / "pain" / "001"
_STATE_DB = _BASE_DIR / "state.db"

_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
)


def _t(el: etree._Element | None) -> str | None:
    return el.text.strip() if el is not None and el.text else None


def _attr(el: etree._Element | None, attr: str) -> str | None:
    return el.get(attr) if el is not None else None


def _iban_country(iban: str | None) -> str | None:
    return iban[:2].upper() if iban and len(iban) >= 2 else None


def _bic_country(bic: str | None) -> str | None:
    return bic[4:6].upper() if bic and len(bic) >= 6 else None


def _file_category(name: str) -> str:
    if name.startswith("gen-pass"):
        return "gen-pass"
    if name.startswith("gen-fail"):
        return "gen-fail"
    if name.startswith("gen-edge"):
        return "gen-edge"
    return "hand-crafted"


def _parse_xml(path: Path) -> list[dict[str, Any]]:
    try:
        tree = etree.parse(str(path), _PARSER)
    except etree.XMLSyntaxError:
        return []

    root = tree.getroot()
    rows: list[dict[str, Any]] = []
    file_name = path.name
    category = _file_category(file_name)

    grp = root.find(".//p:GrpHdr", _NS_MAP)
    msg_id = _t(grp.find("p:MsgId", _NS_MAP)) if grp is not None else None
    nb_txs_raw = _t(grp.find("p:NbOfTxs", _NS_MAP)) if grp is not None else None
    ctrl_sum_raw = _t(grp.find("p:CtrlSum", _NS_MAP)) if grp is not None else None
    initg_pty = _t(grp.find(".//p:InitgPty/p:Nm", _NS_MAP)) if grp is not None else None

    nb_txs = int(nb_txs_raw) if nb_txs_raw and nb_txs_raw.isdigit() else None
    try:
        ctrl_sum = float(ctrl_sum_raw) if ctrl_sum_raw else None
    except ValueError:
        ctrl_sum = None

    for pmt in root.findall(".//p:PmtInf", _NS_MAP):
        pmt_id = _t(pmt.find("p:PmtInfId", _NS_MAP))
        pmt_mtd = _t(pmt.find("p:PmtMtd", _NS_MAP))
        req_date = _t(pmt.find(".//p:ReqdExctnDt/p:Dt", _NS_MAP))

        dbtr_nm = _t(pmt.find(".//p:Dbtr/p:Nm", _NS_MAP))
        dbtr_ctry = _t(pmt.find(".//p:Dbtr/p:PstlAdr/p:Ctry", _NS_MAP))
        dbtr_iban = _t(pmt.find(".//p:DbtrAcct/p:Id/p:IBAN", _NS_MAP))
        dbtr_bic = _t(pmt.find(".//p:DbtrAgt/p:FinInstnId/p:BICFI", _NS_MAP))

        for tx in pmt.findall(".//p:CdtTrfTxInf", _NS_MAP):
            e2e = _t(tx.find(".//p:PmtId/p:EndToEndId", _NS_MAP))
            instr_id = _t(tx.find(".//p:PmtId/p:InstrId", _NS_MAP))
            uetr = _t(tx.find(".//p:PmtId/p:UETR", _NS_MAP))
            chrg_br = _t(tx.find("p:ChrgBr", _NS_MAP))

            inst_el = tx.find(".//p:Amt/p:InstdAmt", _NS_MAP)
            eqvt_el = tx.find(".//p:Amt/p:EqvtAmt/p:Amt", _NS_MAP)
            amt_el = inst_el if inst_el is not None else eqvt_el
            try:
                amount = float(amt_el.text) if amt_el is not None and amt_el.text else None
            except ValueError:
                amount = None
            ccy = _attr(inst_el, "Ccy") or _t(tx.find(".//p:Amt/p:EqvtAmt/p:CcyOfTrf", _NS_MAP))

            cdtr_nm = _t(tx.find(".//p:Cdtr/p:Nm", _NS_MAP))
            cdtr_ctry = _t(tx.find(".//p:Cdtr/p:PstlAdr/p:Ctry", _NS_MAP))
            cdtr_iban = _t(tx.find(".//p:CdtrAcct/p:Id/p:IBAN", _NS_MAP))
            cdtr_bic = _t(tx.find(".//p:CdtrAgt/p:FinInstnId/p:BICFI", _NS_MAP))

            purp = _t(tx.find(".//p:Purp/p:Cd", _NS_MAP)) or _t(tx.find(".//p:Purp/p:Prtry", _NS_MAP))
            rmt_ustrd = _t(tx.find(".//p:RmtInf/p:Ustrd", _NS_MAP))

            rows.append({
                "file_name": file_name,
                "category": category,
                "msg_id": msg_id,
                "nb_txs_declared": nb_txs,
                "ctrl_sum": ctrl_sum,
                "initiating_party": initg_pty,
                "pmt_inf_id": pmt_id,
                "pmt_method": pmt_mtd,
                "req_exec_date": req_date,
                "debtor_name": dbtr_nm,
                "debtor_country": dbtr_ctry or _iban_country(dbtr_iban),
                "debtor_iban_prefix": _iban_country(dbtr_iban),
                "debtor_bic": dbtr_bic,
                "debtor_bic_country": _bic_country(dbtr_bic),
                "end_to_end_id": e2e,
                "instr_id": instr_id,
                "has_uetr": uetr is not None,
                "amount": amount,
                "currency": ccy,
                "charge_bearer": chrg_br,
                "creditor_name": cdtr_nm,
                "creditor_country": cdtr_ctry or _iban_country(cdtr_iban),
                "creditor_iban_prefix": _iban_country(cdtr_iban),
                "creditor_bic": cdtr_bic,
                "creditor_bic_country": _bic_country(cdtr_bic),
                "purpose_code": purp,
                "has_remittance_info": rmt_ustrd is not None,
            })

    if not rows:
        rows.append({k: None for k in [
            "file_name", "category", "msg_id", "nb_txs_declared", "ctrl_sum",
            "initiating_party", "pmt_inf_id", "pmt_method", "req_exec_date",
            "debtor_name", "debtor_country", "debtor_iban_prefix", "debtor_bic",
            "debtor_bic_country", "end_to_end_id", "instr_id", "has_uetr",
            "amount", "currency", "charge_bearer", "creditor_name",
            "creditor_country", "creditor_iban_prefix", "creditor_bic",
            "creditor_bic_country", "purpose_code", "has_remittance_info",
        ] } | {"file_name": file_name, "category": category})

    return rows


def _load_xml_rows() -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    for xml_path in sorted(_XML_DIR.glob("*.xml")):
        all_rows.extend(_parse_xml(xml_path))
    return all_rows


def _hr(title: str) -> None:
    width = 72
    print(f"\n{'═' * width}")
    print(f"  {title}")
    print(f"{'═' * width}")


def _print_table(rel: duckdb.DuckDBPyRelation, indent: int = 2) -> None:
    df = rel.df()
    if df.empty:
        print(" " * indent + "(no rows)")
        return
    pad = " " * indent
    col_widths = {c: max(len(str(c)), df[c].astype(str).str.len().max()) for c in df.columns}
    header = "  ".join(str(c).ljust(col_widths[c]) for c in df.columns)
    sep = "  ".join("─" * col_widths[c] for c in df.columns)
    print(pad + header)
    print(pad + sep)
    for _, row in df.iterrows():
        print(pad + "  ".join(str(row[c]).ljust(col_widths[c]) for c in df.columns))


def main() -> None:
    print("Loading XML files…", end=" ", flush=True)
    rows = _load_xml_rows()
    print(f"{len(rows)} transaction rows from {len(set(r['file_name'] for r in rows))} files")

    con = duckdb.connect()

    df_pain = pd.DataFrame(rows)
    con.register("rows_view", df_pain)
    con.execute("CREATE TABLE pain001 AS SELECT * FROM rows_view")
    con.execute(f"ATTACH '{_STATE_DB}' AS state (TYPE sqlite, READ_ONLY)")
    con.execute("""
        CREATE VIEW pain001_pipeline AS
        SELECT
            p.*,
            pm.validation_status,
            pm.error_detail,
            pm.processed_at,
            sm.sent_at,
            epoch_ms(
                CAST(epoch_ms(strptime(pm.processed_at, '%Y-%m-%dT%H:%M:%S.%f')) AS BIGINT) -
                CAST(epoch_ms(strptime(sm.sent_at,      '%Y-%m-%dT%H:%M:%S.%f')) AS BIGINT)
            ) AS latency_ms_epoch
        FROM pain001 p
        LEFT JOIN state.processed_messages pm ON pm.file_name = p.file_name AND pm.message_set = '001'
        LEFT JOIN state.sent_messages sm      ON sm.file_name = p.file_name AND sm.message_set = '001'
    """)

    # ── 1. Overview ──────────────────────────────────────────────────────
    _hr("1 · OVERVIEW")
    _print_table(con.sql("""
        SELECT
            COUNT(DISTINCT file_name)                               AS total_files,
            COUNT(*)                                                AS total_tx_rows,
            ROUND(AVG(amount) FILTER (WHERE amount IS NOT NULL), 2) AS avg_amount,
            ROUND(SUM(amount) FILTER (WHERE amount IS NOT NULL), 2) AS total_value,
            COUNT(DISTINCT currency)                                AS currencies,
            COUNT(*) FILTER (WHERE has_uetr)                        AS with_uetr,
            COUNT(*) FILTER (WHERE has_remittance_info)             AS with_remittance
        FROM pain001
    """))

    # ── 2. Category vs pipeline outcome ──────────────────────────────────
    _hr("2 · FILE CATEGORY vs PIPELINE OUTCOME")
    _print_table(con.sql("""
        SELECT
            category,
            COUNT(DISTINCT file_name)                                           AS files,
            COUNT(DISTINCT file_name) FILTER (WHERE validation_status='pass')   AS pipeline_pass,
            COUNT(DISTINCT file_name) FILTER (WHERE validation_status='fail')   AS pipeline_fail,
            COUNT(DISTINCT file_name) FILTER (WHERE validation_status IS NULL)  AS not_sent,
            ROUND(
                100.0 * COUNT(DISTINCT file_name) FILTER (WHERE validation_status='pass')
                / NULLIF(COUNT(DISTINCT file_name) FILTER (WHERE validation_status IS NOT NULL), 0)
            , 1)                                                                AS pass_pct
        FROM pain001_pipeline
        GROUP BY category
        ORDER BY category
    """))

    # ── 3. Currency distribution ─────────────────────────────────────────
    _hr("3 · CURRENCY DISTRIBUTION")
    _print_table(con.sql("""
        SELECT
            COALESCE(currency, '(missing)') AS currency,
            COUNT(*)                         AS tx_count,
            ROUND(SUM(amount), 2)            AS total_value,
            ROUND(MIN(amount), 2)            AS min_amt,
            ROUND(MAX(amount), 2)            AS max_amt,
            ROUND(AVG(amount), 2)            AS avg_amt
        FROM pain001
        WHERE amount IS NOT NULL
        GROUP BY currency
        ORDER BY tx_count DESC
    """))

    # ── 4. Amount distribution buckets ───────────────────────────────────
    _hr("4 · AMOUNT DISTRIBUTION BUCKETS  (passing files only)")
    _print_table(con.sql("""
        SELECT
            CASE
                WHEN amount < 100        THEN '< 100'
                WHEN amount < 1000       THEN '100 – 999'
                WHEN amount < 10000      THEN '1k – 9,999'
                WHEN amount < 100000     THEN '10k – 99,999'
                WHEN amount < 1000000    THEN '100k – 999,999'
                ELSE                          '≥ 1M'
            END                           AS bucket,
            COUNT(*)                      AS tx_count,
            ROUND(AVG(amount), 2)         AS avg_amount,
            ROUND(SUM(amount), 2)         AS total_value
        FROM pain001_pipeline
        WHERE amount IS NOT NULL AND validation_status = 'pass'
        GROUP BY bucket
        ORDER BY MIN(amount)
    """))

    # ── 5. Debtor country analysis ────────────────────────────────────────
    _hr("5 · DEBTOR COUNTRY  (from IBAN prefix)")
    _print_table(con.sql("""
        SELECT
            COALESCE(debtor_iban_prefix, '(unknown)')  AS debtor_country,
            COUNT(*)                                    AS tx_count,
            ROUND(SUM(amount), 2)                       AS total_value,
            COUNT(DISTINCT creditor_iban_prefix)        AS unique_dest_countries
        FROM pain001
        WHERE amount IS NOT NULL
        GROUP BY COALESCE(debtor_iban_prefix, '(unknown)')
        ORDER BY tx_count DESC
        LIMIT 15
    """))

    # ── 6. Cross-border flow matrix ───────────────────────────────────────
    _hr("6 · CROSS-BORDER FLOW MATRIX  (debtor country → creditor country)")
    _print_table(con.sql("""
        SELECT
            COALESCE(debtor_iban_prefix,   '??') AS from_country,
            COALESCE(creditor_iban_prefix, '??') AS to_country,
            COUNT(*)                              AS tx_count,
            ROUND(SUM(amount), 2)                 AS total_value,
            CASE
                WHEN debtor_iban_prefix IS NOT DISTINCT FROM creditor_iban_prefix
                THEN 'domestic'
                ELSE 'cross-border'
            END                                   AS flow_type
        FROM pain001
        WHERE amount IS NOT NULL
          AND debtor_iban_prefix IS NOT NULL
          AND creditor_iban_prefix IS NOT NULL
        GROUP BY from_country, to_country, flow_type
        ORDER BY tx_count DESC
        LIMIT 20
    """))

    # ── 7. Charge bearer distribution ────────────────────────────────────
    _hr("7 · CHARGE BEARER (ChrgBr) DISTRIBUTION")
    _print_table(con.sql("""
        SELECT
            COALESCE(charge_bearer, '(absent)') AS charge_bearer,
            COUNT(*)                             AS tx_count,
            ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct,
            ROUND(AVG(amount), 2)                AS avg_amount
        FROM pain001
        GROUP BY charge_bearer
        ORDER BY tx_count DESC
    """))

    # ── 8. Payment method split ───────────────────────────────────────────
    _hr("8 · PAYMENT METHOD (PmtMtd)")
    _print_table(con.sql("""
        SELECT
            COALESCE(pmt_method, '(absent)') AS pmt_method,
            COUNT(DISTINCT file_name)         AS files,
            COUNT(*)                          AS tx_count
        FROM pain001
        GROUP BY pmt_method
        ORDER BY tx_count DESC
    """))

    # ── 9. Purpose code usage ─────────────────────────────────────────────
    _hr("9 · PURPOSE CODE USAGE")
    _print_table(con.sql("""
        SELECT
            COALESCE(purpose_code, '(none)') AS purpose_code,
            COUNT(*)                          AS tx_count,
            ROUND(SUM(amount), 2)             AS total_value
        FROM pain001
        GROUP BY purpose_code
        ORDER BY tx_count DESC
        LIMIT 15
    """))

    # ── 10. UETR and remittance coverage ─────────────────────────────────
    _hr("10 · ENRICHMENT COVERAGE  (UETR · Remittance · Purpose)")
    _print_table(con.sql("""
        SELECT
            category,
            COUNT(*)                                                        AS tx_count,
            ROUND(100.0 * COUNT(*) FILTER (WHERE has_uetr) / COUNT(*), 1)              AS uetr_pct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE has_remittance_info) / COUNT(*), 1)   AS remittance_pct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE purpose_code IS NOT NULL) / COUNT(*), 1) AS purpose_pct
        FROM pain001
        GROUP BY category
        ORDER BY category
    """))

    # ── 11. Top XSD error patterns ────────────────────────────────────────
    _hr("11 · TOP XSD VALIDATION ERROR PATTERNS  (pain.001 failures)")
    _print_table(con.sql("""
        SELECT
            REGEXP_EXTRACT(error_detail, '[}]([A-Za-z]+)['':]', 1) AS failing_element,
            COUNT(*)                       AS occurrence,
            ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_failures
        FROM state.processed_messages
        WHERE message_set = '001'
          AND validation_status = 'fail'
          AND error_detail IS NOT NULL
        GROUP BY failing_element
        ORDER BY occurrence DESC
        LIMIT 12
    """))

    # ── 12. Error type classification ─────────────────────────────────────
    _hr("12 · ERROR TYPE CLASSIFICATION")
    _print_table(con.sql("""
        SELECT
            CASE
                WHEN error_detail LIKE '%is not expected%'       THEN 'Wrong element order / unexpected element'
                WHEN error_detail LIKE '%Missing child element%' THEN 'Missing required element'
                WHEN error_detail LIKE '%not a valid value%'     THEN 'Invalid field value'
                WHEN error_detail LIKE '%minLength%'
                  OR error_detail LIKE '%maxLength%'             THEN 'String length constraint'
                WHEN error_detail LIKE '%pattern%'               THEN 'Pattern / regex mismatch'
                WHEN error_detail LIKE '%facet%'                 THEN 'Facet constraint violation'
                ELSE 'Other'
            END                            AS error_type,
            COUNT(*)                       AS count,
            ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
        FROM state.processed_messages
        WHERE message_set = '001' AND validation_status = 'fail'
        GROUP BY error_type
        ORDER BY count DESC
    """))

    # ── 13. Declared vs actual transaction count ──────────────────────────
    _hr("13 · DECLARED NbOfTxs vs ACTUAL PARSED TRANSACTIONS")
    _print_table(con.sql("""
        WITH per_file AS (
            SELECT
                file_name,
                category,
                MAX(nb_txs_declared)  AS declared,
                COUNT(*)              AS actual_parsed,
                validation_status
            FROM pain001_pipeline
            GROUP BY file_name, category, validation_status
        )
        SELECT
            category,
            validation_status,
            COUNT(*)                                              AS files,
            SUM(declared)                                         AS total_declared,
            SUM(actual_parsed)                                    AS total_parsed,
            COUNT(*) FILTER (WHERE declared != actual_parsed)    AS mismatch_files
        FROM per_file
        GROUP BY category, validation_status
        ORDER BY category, validation_status
    """))

    # ── 14. Debtor BIC country distribution ───────────────────────────────
    _hr("14 · DEBTOR BANK COUNTRY  (from BIC)")
    _print_table(con.sql("""
        SELECT
            COALESCE(debtor_bic_country, '(no BIC)') AS bank_country,
            COUNT(*)                                   AS tx_count,
            COUNT(DISTINCT debtor_bic)                 AS unique_bics
        FROM pain001
        GROUP BY bank_country
        ORDER BY tx_count DESC
        LIMIT 12
    """))

    # ── 15. High-value transaction outliers ───────────────────────────────
    _hr("15 · HIGH-VALUE TRANSACTIONS  (top 10 by amount, passing only)")
    _print_table(con.sql("""
        SELECT
            file_name,
            creditor_name,
            COALESCE(creditor_iban_prefix, '??') AS cdtr_ctry,
            currency,
            ROUND(amount, 2)                     AS amount,
            charge_bearer,
            COALESCE(purpose_code, '—')          AS purpose
        FROM pain001_pipeline
        WHERE validation_status = 'pass' AND amount IS NOT NULL
        ORDER BY amount DESC
        LIMIT 10
    """))

    print("\n" + "═" * 72)
    print("  Done.")
    print("═" * 72 + "\n")


if __name__ == "__main__":
    main()
