# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.0,<2.0", "lxml>=5.0", "pandas>=2.0"]
# ///
"""Generate a Thoughtworks-branded HTML analytics report for all pain message sets."""

from __future__ import annotations

import html as _html_lib
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from lxml import etree

_BASE_DIR  = Path(__file__).parent
_TEST_DATA = _BASE_DIR / "test_data" / "pain"
_STATE_DB  = _BASE_DIR / "state.db"
_REPORTS   = _BASE_DIR / "reports"

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

_MESSAGE_SETS = ["001", "002", "007", "008", "009", "010", "011", "012", "013", "014", "017", "018"]

_MS_LABELS = {
    "001": "Customer Credit Transfer Initiation",
    "002": "Customer Payment Status Report",
    "007": "Customer Payment Reversal",
    "008": "Customer Direct Debit Initiation",
    "009": "Mandate Initiation Request",
    "010": "Mandate Amendment Request",
    "011": "Mandate Cancellation Request",
    "012": "Mandate Acceptance Report",
    "013": "Creditor Payment Activation Request",
    "014": "Creditor Payment Activation Status Report",
    "017": "Mandate Copy Request",
    "018": "Mandate Suspension Request",
}

_MS_COLOR = {
    "001": "coral", "002": "amber", "007": "plum",
    "008": "coral", "009": "green", "010": "green",
    "011": "green", "012": "green", "013": "coral",
    "014": "amber", "017": "green", "018": "green",
}

_SIDEBAR_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Overview",           [("overview", "Executive Summary")]),
    ("Payment Initiation", [("001", "Credit Transfer"), ("008", "Direct Debit"), ("013", "Cdtr Activation")]),
    ("Status Reports",     [("002", "Payment Status"), ("014", "Activation Status")]),
    ("Reversals",          [("007", "Payment Reversal")]),
    ("Mandate Management", [("009", "Initiation"), ("010", "Amendment"), ("011", "Cancellation"),
                            ("012", "Acceptance"), ("017", "Copy Request"), ("018", "Suspension")]),
]


# ── schema helpers ─────────────────────────────────────────────────────────

def _schema_version(ms: str) -> str:
    xsds = list((_BASE_DIR / "schema" / "pain" / ms).glob("*.xsd"))
    return xsds[0].stem if xsds else f"pain.{ms}"


# ── XML field helpers ──────────────────────────────────────────────────────

def _text(el: etree._Element | None) -> str | None:
    return el.text.strip() if el is not None and el.text else None


def _attr(el: etree._Element | None, name: str) -> str | None:
    return el.get(name) if el is not None else None


def _iban_pfx(iban: str | None) -> str | None:
    return iban[:2].upper() if iban and len(iban) >= 2 else None


_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("gen-pass", "gen-pass"),
    ("gen-fail", "gen-fail"),
    ("gen-edge", "gen-edge"),
]


def _category(fname: str) -> str:
    for prefix, label in _CATEGORY_PREFIXES:
        if fname.startswith(prefix):
            return label
    return "hand-crafted"


# ── per-message-set extractors ─────────────────────────────────────────────

def _ext_001_013(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pmt in root.findall(".//p:PmtInf", nsm):
        di  = _text(pmt.find(".//p:DbtrAcct/p:Id/p:IBAN", nsm))
        db  = _text(pmt.find(".//p:DbtrAgt/p:FinInstnId/p:BICFI", nsm))
        pm  = _text(pmt.find("p:PmtMtd", nsm))
        for tx in pmt.findall(".//p:CdtTrfTxInf", nsm):
            ie = tx.find(".//p:Amt/p:InstdAmt", nsm)
            try:
                amt: float | None = float(ie.text) if ie is not None and ie.text else None
            except (ValueError, TypeError):
                amt = None
            ci = _text(tx.find(".//p:CdtrAcct/p:Id/p:IBAN", nsm))
            rows.append({
                "file_name": fname, "category": cat, "pmt_method": pm,
                "debtor_pfx": _iban_pfx(di), "debtor_bic": db,
                "amount": amt, "currency": _attr(ie, "Ccy") if ie is not None else None,
                "creditor_pfx": _iban_pfx(ci),
                "charge_bearer": _text(tx.find("p:ChrgBr", nsm)),
                "purpose": _text(tx.find(".//p:Purp/p:Cd", nsm)),
                "has_uetr": tx.find(".//p:PmtId/p:UETR", nsm) is not None,
            })
    _null: dict[str, Any] = {
        "file_name": fname, "category": cat, "pmt_method": None,
        "debtor_pfx": None, "debtor_bic": None, "amount": None, "currency": None,
        "creditor_pfx": None, "charge_bearer": None, "purpose": None, "has_uetr": False,
    }
    return rows or [_null]


def _ext_002_014(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for o in root.findall(".//p:OrgnlGrpInfAndSts", nsm):
        rows.append({"file_name": fname, "category": cat,
                     "grp_status": _text(o.find("p:GrpSts", nsm)), "tx_status": None, "reason_code": None})
    for tx in root.findall(".//p:TxInfAndSts", nsm):
        rows.append({"file_name": fname, "category": cat, "grp_status": None,
                     "tx_status": _text(tx.find("p:TxSts", nsm)),
                     "reason_code": _text(tx.find(".//p:StsRsnInf/p:Rsn/p:Cd", nsm))})
    _null: dict[str, Any] = {"file_name": fname, "category": cat,
                              "grp_status": None, "tx_status": None, "reason_code": None}
    return rows or [_null]


def _ext_007(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tx in root.findall(".//p:TxInf", nsm):
        ae = tx.find(".//p:OrgnlInstdAmt", nsm)
        try:
            amt: float | None = float(ae.text) if ae is not None and ae.text else None
        except (ValueError, TypeError):
            amt = None
        rows.append({"file_name": fname, "category": cat, "orig_amount": amt,
                     "currency": _attr(ae, "Ccy") if ae is not None else None,
                     "reversal_reason": _text(tx.find(".//p:RvslRsnInf/p:Rsn/p:Cd", nsm))})
    _null: dict[str, Any] = {"file_name": fname, "category": cat,
                              "orig_amount": None, "currency": None, "reversal_reason": None}
    return rows or [_null]


def _ext_008(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pmt in root.findall(".//p:PmtInf", nsm):
        ci = _text(pmt.find(".//p:CdtrAcct/p:Id/p:IBAN", nsm))
        for tx in pmt.findall(".//p:DrctDbtTxInf", nsm):
            ae = tx.find(".//p:InstdAmt", nsm)
            try:
                amt: float | None = float(ae.text) if ae is not None and ae.text else None
            except (ValueError, TypeError):
                amt = None
            di = _text(tx.find(".//p:DbtrAcct/p:Id/p:IBAN", nsm))
            rows.append({"file_name": fname, "category": cat,
                         "amount": amt, "currency": _attr(ae, "Ccy") if ae is not None else None,
                         "debtor_pfx": _iban_pfx(di), "creditor_pfx": _iban_pfx(ci)})
    _null: dict[str, Any] = {"file_name": fname, "category": cat,
                              "amount": None, "currency": None, "debtor_pfx": None, "creditor_pfx": None}
    return rows or [_null]


def _ext_009(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m in root.findall(".//p:Mndt", nsm):
        ci = _text(m.find(".//p:CdtrAcct/p:Id/p:IBAN", nsm))
        di = _text(m.find(".//p:DbtrAcct/p:Id/p:IBAN", nsm))
        rows.append({"file_name": fname, "category": cat,
                     "tracking_ind": _text(m.find("p:TrckgInd", nsm)),
                     "creditor_pfx": _iban_pfx(ci), "debtor_pfx": _iban_pfx(di)})
    _null: dict[str, Any] = {"file_name": fname, "category": cat,
                              "tracking_ind": None, "creditor_pfx": None, "debtor_pfx": None}
    return rows or [_null]


def _ext_010(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows = [{"file_name": fname, "category": cat,
             "amendment_reason": _text(a.find(".//p:AmdmntRsn/p:Rsn/p:Cd", nsm))}
            for a in root.findall(".//p:UndrlygAmdmntDtls", nsm)]
    return rows or [{"file_name": fname, "category": cat, "amendment_reason": None}]


def _ext_011(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows = [{"file_name": fname, "category": cat,
             "cancellation_reason": _text(c.find(".//p:CxlRsn/p:Rsn/p:Cd", nsm))}
            for c in root.findall(".//p:UndrlygCxlDtls", nsm)]
    return rows or [{"file_name": fname, "category": cat, "cancellation_reason": None}]


def _ext_012(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows = [{"file_name": fname, "category": cat,
             "accepted": _text(a.find(".//p:AccptncRslt/p:Accptd", nsm)),
             "rejection_reason": _text(a.find(".//p:AccptncRslt/p:RjctRsn/p:Rsn/p:Cd", nsm))}
            for a in root.findall(".//p:UndrlygAccptncDtls", nsm)]
    return rows or [{"file_name": fname, "category": cat, "accepted": None, "rejection_reason": None}]


def _ext_017(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows = [{"file_name": fname, "category": cat,
             "orig_mandate_id": _text(c.find(".//p:OrgnlMndt/p:OrgnlMndtId", nsm))}
            for c in root.findall(".//p:UndrlygCpyReqDtls", nsm)]
    return rows or [{"file_name": fname, "category": cat, "orig_mandate_id": None}]


def _ext_018(root: etree._Element, nsm: dict[str, str], fname: str, cat: str) -> list[dict[str, Any]]:
    rows = [{"file_name": fname, "category": cat,
             "suspension_reason": _text(s.find(".//p:SspnsnRsn/p:Rsn/p:Cd", nsm))}
            for s in root.findall(".//p:UndrlygSspnsnDtls", nsm)]
    return rows or [{"file_name": fname, "category": cat, "suspension_reason": None}]


_EXTRACTORS: dict[str, Any] = {
    "001": _ext_001_013, "002": _ext_002_014, "007": _ext_007,
    "008": _ext_008,     "009": _ext_009,     "010": _ext_010,
    "011": _ext_011,     "012": _ext_012,     "013": _ext_001_013,
    "014": _ext_002_014, "017": _ext_017,     "018": _ext_018,
}


def _load_ms(ms: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    base:   list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    for path in sorted((_TEST_DATA / ms).glob("*.xml")):
        try:
            tree = etree.parse(str(path), _PARSER)
        except etree.XMLSyntaxError:
            continue
        root = tree.getroot()
        tag  = root.tag
        ns   = tag[1:tag.index("}")] if tag.startswith("{") else ""
        nsm  = {"p": ns}
        grp  = root.find(".//p:GrpHdr", nsm)
        cat  = _category(path.name)
        base.append({
            "file_name":   path.name,
            "message_set": ms,
            "category":    cat,
            "msg_id":      _text(grp.find("p:MsgId", nsm)) if grp is not None else None,
        })
        detail.extend(_EXTRACTORS[ms](root, nsm, path.name, cat))
    cols_b = ["file_name", "message_set", "category", "msg_id"]
    bdf = pd.DataFrame(base)   if base   else pd.DataFrame(columns=cols_b)
    ddf = pd.DataFrame(detail) if detail else pd.DataFrame()
    return bdf, ddf


# ── DuckDB helper ──────────────────────────────────────────────────────────

def _run_query(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    try:
        return con.sql(sql).df()
    except duckdb.Error:
        return pd.DataFrame()


# ── HTML/string helpers ────────────────────────────────────────────────────

def _esc(v: Any) -> str:
    return _html_lib.escape(str(v)) if v is not None else "—"


def _stat(label: str, value: Any, color: str = "var(--teal)") -> str:
    return (
        f'<div class="stat-card">'
        f'<div class="stat-top" style="background:{color}"></div>'
        f'<div class="stat-body">'
        f'<div class="stat-num">{_esc(value)}</div>'
        f'<div class="stat-lbl">{_esc(label)}</div>'
        f'</div></div>'
    )


def _table(df: pd.DataFrame, hi: str | None = None) -> str:
    if df is None or df.empty:
        return '<p class="nd">No data.</p>'
    ths  = "".join(f"<th>{_esc(c)}</th>" for c in df.columns)
    body = ""
    for _, r in df.iterrows():
        tds = ""
        for c in df.columns:
            v  = r[c]
            cl = ""
            if hi and c == hi:
                s = str(v).lower() if v is not None else ""
                if s in ("pass", "accp", "true"):
                    cl = ' class="cg"'
                elif s in ("fail", "rjct", "false"):
                    cl = ' class="cr"'
            tds += f"<td{cl}>{_esc(v)}</td>"
        body += f"<tr>{tds}</tr>"
    return f'<table class="dt"><thead><tr>{ths}</tr></thead><tbody>{body}</tbody></table>'


def _hbars(rows: list[tuple[str, int | float, int | float, str]]) -> str:
    parts = []
    for label, value, max_val, color in rows:
        pct    = round(100 * float(value) / float(max_val), 1) if max_val else 0
        inside = str(int(value)) if pct >= 12 else ""
        outside = f"{int(value)}" if pct < 12 else ""
        parts.append(
            f'<div class="hbr">'
            f'<span class="hbl">{_esc(label)}</span>'
            f'<div class="hbt"><div class="hbb" style="width:{pct}%;background:{color}">{inside}</div></div>'
            f'<span class="hbv">{outside} {pct}%</span>'
            f'</div>'
        )
    return '<div class="hbc">' + "".join(parts) + "</div>"


def _pass_fail_bar(passed: int, failed: int) -> str:
    total = passed + failed or 1
    pp = round(100 * passed / total, 0)
    fp = 100 - pp
    return (
        f'<div class="pfbar">'
        f'<div style="width:{pp}%;background:var(--green)"></div>'
        f'<div style="width:{fp}%;background:var(--coral)"></div>'
        f'</div>'
        f'<div class="pfleg">'
        f'<span class="leg-g">&#9679; Pass {passed}</span>'
        f'<span class="leg-r">&#9679; Fail {failed}</span>'
        f'</div>'
    )


def _cat_cards(bdf: pd.DataFrame) -> str:
    cats = bdf["category"].value_counts().to_dict() if not bdf.empty else {}
    def card(cls: str, label: str, key: str) -> str:
        return (f'<div class="cat-card {cls}">'
                f'<div class="cn">{cats.get(key, 0)}</div>'
                f'<div class="cl">{label}</div></div>')
    return (
        '<div class="cat-grid">'
        + card("gp", "gen-pass",     "gen-pass")
        + card("gf", "gen-fail",     "gen-fail")
        + card("ge", "gen-edge",     "gen-edge")
        + card("hc", "hand-crafted", "hand-crafted")
        + '</div>'
    )


# ── CSS ────────────────────────────────────────────────────────────────────

_CSS = """
:root{--white:#FFFFFF;--mist:#EDF1F3;--black:#000000;--teal-dk:#003D4F;--coral:#F2617A;--amber:#CC850A;--green:#689E78;--teal:#47A1AD;--plum:#634F7D;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',sans-serif;background:var(--mist);color:var(--black);display:flex;min-height:100vh;font-size:14px;line-height:1.5;}

/* ── Sidebar ── */
.sb{width:240px;min-width:240px;background:var(--teal-dk);position:fixed;top:0;left:0;height:100vh;overflow-y:auto;z-index:100;}
.sb-brand{padding:1.1rem 1.2rem 0.9rem;border-bottom:1px solid rgba(255,255,255,0.1);}
.sb-brand .bn{color:#fff;font-family:'Bitter',serif;font-size:1rem;font-weight:700;line-height:1.2;}
.sb-brand .bs{color:var(--teal);font-size:0.64rem;margin-top:0.25rem;}
.sb-grp{padding:0.6rem 0 0.2rem;}
.sb-grp-title{color:rgba(255,255,255,0.32);font-size:0.57rem;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;padding:0 1.2rem 0.35rem;display:block;}
.sb a{display:flex;align-items:center;gap:0.45rem;padding:0.33rem 1.2rem;color:rgba(255,255,255,0.68);font-size:0.77rem;text-decoration:none;transition:background 0.12s;}
.sb a:hover{background:rgba(255,255,255,0.08);color:#fff;}
.sb a.ov-lnk{color:rgba(255,255,255,0.88);font-weight:600;}
.ms-tag{display:inline-block;background:rgba(71,161,173,0.22);color:var(--teal);font-size:0.57rem;font-weight:700;padding:0.04rem 0.3rem;border-radius:3px;min-width:26px;text-align:center;flex-shrink:0;}

/* ── Main ── */
.mw{margin-left:240px;flex:1;min-width:0;display:flex;flex-direction:column;}
.ph{background:var(--teal-dk);padding:1.5rem 2.2rem 1.3rem;border-bottom:4px solid var(--coral);position:sticky;top:0;z-index:50;}
.ph .ey{font-size:0.61rem;font-weight:700;letter-spacing:0.16em;text-transform:uppercase;color:var(--teal);margin-bottom:0.28rem;}
.ph h1{font-family:'Bitter',serif;color:#fff;font-size:1.5rem;font-weight:700;}
.ph .mt{color:rgba(255,255,255,0.4);font-size:0.7rem;margin-top:0.3rem;}
.content{max-width:940px;margin:0 auto;padding:1.8rem 2rem 3rem;width:100%;}

/* ── Sections ── */
.sec{margin-bottom:3rem;scroll-margin-top:95px;}
.sh{display:flex;align-items:baseline;gap:0.75rem;flex-wrap:wrap;padding:0.72rem 1rem;background:#fff;border-left:5px solid var(--teal);border-radius:0 8px 8px 0;margin-bottom:1.1rem;}
.sh.coral{border-color:var(--coral);}
.sh.amber{border-color:var(--amber);}
.sh.plum {border-color:var(--plum);}
.sh.green{border-color:var(--green);}
.sh h2{font-family:'Bitter',serif;color:var(--teal-dk);font-size:1.08rem;margin:0;}
.schema-badge{background:var(--mist);border:1px solid #c5cfd5;color:#5a6a73;font-size:0.61rem;font-weight:700;letter-spacing:0.04em;padding:0.12rem 0.45rem;border-radius:3px;white-space:nowrap;}
.ms-lbl{color:#7a8f98;font-size:0.74rem;}

/* ── Sub-panels ── */
.sub{background:#fff;border-radius:8px;padding:0.95rem 1.15rem;margin-bottom:0.85rem;}
.sub h3{font-family:'Bitter',serif;color:var(--teal-dk);font-size:0.83rem;margin-bottom:0.7rem;padding-bottom:0.38rem;border-bottom:1px solid var(--mist);}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:0.85rem;}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.85rem;}

/* ── Stat cards ── */
.sc-row{display:flex;gap:0.65rem;flex-wrap:wrap;margin-bottom:0.85rem;}
.stat-card{background:#fff;border-radius:8px;overflow:hidden;flex:1;min-width:88px;}
.stat-top{height:4px;}
.stat-body{padding:0.6rem 0.8rem;}
.stat-num{font-family:'Bitter',serif;font-size:1.45rem;font-weight:700;color:var(--teal-dk);line-height:1;}
.stat-lbl{font-size:0.63rem;color:#5a6a73;margin-top:0.18rem;line-height:1.3;}

/* ── Pass/fail bar ── */
.pfbar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin-top:0.45rem;}
.pfleg{display:flex;gap:0.8rem;font-size:0.67rem;margin-top:0.22rem;}
.leg-g{color:var(--green);font-weight:600;}
.leg-r{color:var(--coral);font-weight:600;}

/* ── Horizontal bar chart ── */
.hbc{display:flex;flex-direction:column;gap:0.32rem;}
.hbr{display:grid;grid-template-columns:90px 1fr 70px;align-items:center;gap:0.45rem;}
.hbr.wide{grid-template-columns:80px 1fr 80px;}
.hbl{font-size:0.72rem;color:#3a4a52;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.hbt{background:var(--mist);border-radius:3px;height:20px;overflow:hidden;}
.hbb{height:100%;display:flex;align-items:center;padding:0 5px;font-size:0.64rem;color:#fff;font-weight:700;border-radius:3px;min-width:3px;transition:width 0.3s;}
.hbv{font-size:0.67rem;color:#5a6a73;white-space:nowrap;}

/* ── Tables ── */
.dt{width:100%;border-collapse:collapse;font-size:0.75rem;}
.dt th{background:var(--teal-dk);color:#fff;padding:0.38rem 0.65rem;text-align:left;font-weight:600;font-size:0.67rem;letter-spacing:0.04em;text-transform:uppercase;}
.dt td{padding:0.33rem 0.65rem;border-bottom:1px solid #e2eaed;vertical-align:top;}
.dt tr:last-child td{border-bottom:none;}
.dt tr:nth-child(even) td{background:var(--mist);}
.cg{color:var(--green);font-weight:600;}
.cr{color:var(--coral);font-weight:600;}
.nd{color:#8a9aa2;font-size:0.74rem;font-style:italic;padding:0.25rem 0;}

/* ── Overview ── */
.ov-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem;}

/* ── Category cards ── */
.cat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:0.5rem;}
.cat-card{border-radius:6px;padding:0.55rem 0.65rem;text-align:center;background:var(--mist);}
.cat-card .cn{font-family:'Bitter',serif;font-size:1.18rem;font-weight:700;color:var(--teal-dk);}
.cat-card .cl{font-size:0.62rem;color:#5a6a73;margin-top:0.12rem;}
.cat-card.gp{border-top:3px solid var(--green);}
.cat-card.gf{border-top:3px solid var(--coral);}
.cat-card.ge{border-top:3px solid var(--amber);}
.cat-card.hc{border-top:3px solid var(--plum);}

/* ── Error type chips ── */
.ec{display:inline-block;font-size:0.62rem;font-weight:600;padding:0.14rem 0.42rem;border-radius:10px;margin:0.12rem;}
.ec-order  {background:rgba(242,97,122,0.12);color:var(--coral);}
.ec-missing{background:rgba(204,133,10,0.12);color:#7a5200;}
.ec-value  {background:rgba(104,158,120,0.12);color:#3a6e4a;}
.ec-length {background:rgba(71,161,173,0.12);color:#2a6570;}
.ec-pattern{background:rgba(99,79,125,0.12);color:var(--plum);}
.ec-facet  {background:rgba(0,61,79,0.1);color:var(--teal-dk);}
.ec-other  {background:var(--mist);color:#5a6a73;}
"""


# ── Page structure ─────────────────────────────────────────────────────────

def _html_head() -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "  <meta charset=\"UTF-8\"/>\n"
        "  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>\n"
        "  <title>ISO 20022 Pain Analytics — Thoughtworks</title>\n"
        "  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\"/>\n"
        "  <link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin/>\n"
        "  <link href=\"https://fonts.googleapis.com/css2?family=Bitter:wght@400;600;700"
        "&family=Inter:wght@400;500;600;700&display=swap\" rel=\"stylesheet\"/>\n"
        f"  <style>{_CSS}</style>\n"
        "</head>\n<body>"
    )


def _html_sidebar() -> str:
    parts = [
        '<nav class="sb">',
        '<div class="sb-brand">',
        '<div class="bn">Thoughtworks</div>',
        '<div class="bs">ISO 20022 &middot; Pain Analytics</div>',
        '</div>',
    ]
    for grp_name, items in _SIDEBAR_GROUPS:
        parts.append(f'<div class="sb-grp"><span class="sb-grp-title">{_esc(grp_name)}</span>')
        for item_id, item_label in items:
            if item_id == "overview":
                parts.append(f'<a href="#overview" class="ov-lnk">{_esc(item_label)}</a>')
            else:
                parts.append(
                    f'<a href="#s{item_id}">'
                    f'<span class="ms-tag">{item_id}</span>'
                    f'{_esc(item_label)}</a>'
                )
        parts.append("</div>")
    parts.append("</nav>")
    return "".join(parts)


def _html_page_header(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
    return (
        '<div class="mw">'
        '<div class="ph">'
        '<div class="ey">Thoughtworks &middot; Payments Practice</div>'
        '<h1>ISO 20022 Pain Domain &mdash; Test Analytics Report</h1>'
        f'<div class="mt">Generated {dt} &nbsp;&middot;&nbsp; 12 message sets &nbsp;&middot;&nbsp; pain.001&ndash;018</div>'
        '</div>'
        '<div class="content">'
    )


def _html_footer() -> str:
    return "</div></div></body></html>"


# ── Overview section ────────────────────────────────────────────────────────

def _html_overview(con: duckdb.DuckDBPyConnection) -> str:
    totals = _run_query(con, """
        SELECT COUNT(DISTINCT file_name) AS files,
               COUNT(*) AS processed,
               COUNT(*) FILTER (WHERE validation_status='pass') AS passed,
               COUNT(*) FILTER (WHERE validation_status='fail') AS failed
        FROM state.processed_messages WHERE domain='pain'
    """)
    tf    = int(totals.iloc[0]["files"])     if not totals.empty else 0
    tp    = int(totals.iloc[0]["processed"]) if not totals.empty else 0
    tpass = int(totals.iloc[0]["passed"])    if not totals.empty else 0
    tfail = int(totals.iloc[0]["failed"])    if not totals.empty else 0
    pr    = f"{round(100 * tpass / tp, 1)}%" if tp else "—"

    ms_pf = _run_query(con, """
        SELECT message_set,
               COUNT(*) FILTER (WHERE validation_status='pass') AS passed,
               COUNT(*) FILTER (WHERE validation_status='fail') AS failed,
               COUNT(*) AS total
        FROM state.processed_messages WHERE domain='pain'
        GROUP BY message_set ORDER BY message_set
    """)

    pipeline_tbl = _run_query(con, """
        SELECT message_set,
               COUNT(*) FILTER (WHERE validation_status='pass') AS pass_count,
               COUNT(*) FILTER (WHERE validation_status='fail') AS fail_count,
               COUNT(*) AS total,
               ROUND(100.0 * COUNT(*) FILTER (WHERE validation_status='pass') / COUNT(*), 1) AS pass_pct
        FROM state.processed_messages WHERE domain='pain'
        GROUP BY message_set ORDER BY message_set
    """)

    bar_parts: list[str] = []
    if not ms_pf.empty:
        max_total = int(ms_pf["total"].max())
        for _, r in ms_pf.iterrows():
            ms     = r["message_set"]
            passed = int(r["passed"])
            failed = int(r["failed"])
            total  = int(r["total"])
            pp     = round(100 * passed / max_total, 1)
            fp     = round(100 * failed / max_total, 1)
            pr_pct = round(100 * passed / total, 1) if total else 0
            bar_parts.append(
                f'<div class="hbr wide">'
                f'<span class="hbl">pain.{ms}</span>'
                f'<div class="hbt" style="display:flex;">'
                f'<div class="hbb" style="width:{pp}%;background:var(--green);">{passed if pp>=8 else ""}</div>'
                f'<div class="hbb" style="width:{fp}%;background:var(--coral);">{failed if fp>=8 else ""}</div>'
                f'</div>'
                f'<span class="hbv">{pr_pct}%</span>'
                f'</div>'
            )

    stat_html = (
        _stat("Total Files", tf, "var(--teal)")
        + _stat("Sent &amp; Processed", tp, "var(--teal-dk)")
        + _stat("Passed", tpass, "var(--green)")
        + _stat("Failed", tfail, "var(--coral)")
        + _stat("Pass Rate", pr, "var(--green)" if tpass >= tfail else "var(--coral)")
        + _stat("Message Sets", "12", "var(--plum)")
    )

    return (
        '<section class="sec" id="overview">'
        '<div class="sh"><h2>Executive Summary</h2>'
        '<span class="ms-lbl">All 12 pain message sets &middot; pain.001&ndash;018</span></div>'
        f'<div class="sc-row">{stat_html}</div>'
        '<div class="ov-grid">'
        f'<div class="sub"><h3>Pass / Fail by Message Set</h3><div class="hbc">{"".join(bar_parts)}</div></div>'
        f'<div class="sub"><h3>Pipeline Summary</h3>{_table(pipeline_tbl)}</div>'
        "</div>"
        "</section>"
    )


# ── Common per-section blocks ───────────────────────────────────────────────

def _ms_stat_block(con: duckdb.DuckDBPyConnection, ms: str, bdf: pd.DataFrame) -> str:
    pf = _run_query(con, f"""
        SELECT validation_status, COUNT(*) AS cnt
        FROM state.processed_messages WHERE message_set='{ms}' AND domain='pain'
        GROUP BY validation_status
    """)
    d     = dict(zip(pf["validation_status"], pf["cnt"].astype(int))) if not pf.empty else {}
    pa    = d.get("pass", 0)
    fa    = d.get("fail", 0)
    tot   = pa + fa
    pr    = f"{round(100*pa/tot, 1)}%" if tot else "—"
    nf    = len(bdf) if not bdf.empty else 0
    cards = (
        _stat("Files",     nf, "var(--teal)")
        + _stat("Passed",  pa, "var(--green)")
        + _stat("Failed",  fa, "var(--coral)")
        + _stat("Pass Rate", pr, "var(--green)" if pa >= fa else "var(--coral)")
    )
    return f'<div class="sc-row">{cards}</div><div style="margin-bottom:0.85rem;">{_pass_fail_bar(pa, fa)}</div>'


def _error_block(con: duckdb.DuckDBPyConnection, ms: str) -> str:
    top_el = _run_query(con, f"""
        SELECT REGEXP_EXTRACT(error_detail, '[}}]([A-Za-z]+)', 1) AS element,
               COUNT(*) AS count
        FROM state.processed_messages
        WHERE message_set='{ms}' AND validation_status='fail' AND error_detail IS NOT NULL
        GROUP BY element HAVING element != ''
        ORDER BY count DESC LIMIT 8
    """)

    err_types = _run_query(con, f"""
        SELECT
            CASE
                WHEN error_detail LIKE '%is not expected%'         THEN 'Element order'
                WHEN error_detail LIKE '%Missing child element%'   THEN 'Missing required'
                WHEN error_detail LIKE '%not a valid value%'       THEN 'Invalid value'
                WHEN error_detail LIKE '%minLength%'
                  OR error_detail LIKE '%maxLength%'               THEN 'Length constraint'
                WHEN error_detail LIKE '%pattern%'                 THEN 'Pattern mismatch'
                WHEN error_detail LIKE '%facet%'                   THEN 'Facet violation'
                ELSE                                                    'Other'
            END AS error_type,
            COUNT(*) AS count
        FROM state.processed_messages
        WHERE message_set='{ms}' AND validation_status='fail' AND error_detail IS NOT NULL
        GROUP BY error_type ORDER BY count DESC
    """)

    type_cls = {
        "Element order": "ec-order",   "Missing required": "ec-missing",
        "Invalid value": "ec-value",   "Length constraint": "ec-length",
        "Pattern mismatch": "ec-pattern", "Facet violation": "ec-facet",
        "Other": "ec-other",
    }
    pills = ""
    if not err_types.empty:
        for _, r in err_types.iterrows():
            cls   = type_cls.get(str(r["error_type"]), "ec-other")
            pills += f'<span class="ec {cls}">{_esc(r["error_type"])} ({int(r["count"])})</span>'
    else:
        pills = '<p class="nd">No validation failures.</p>'

    el_html = _table(top_el) if not top_el.empty else '<p class="nd">No failure data.</p>'
    return (
        '<div class="g2">'
        f'<div class="sub"><h3>Top Failing XSD Elements</h3>{el_html}</div>'
        f'<div class="sub"><h3>Error Type Classification</h3><div style="padding-top:0.2rem;">{pills}</div></div>'
        "</div>"
    )


# ── Message-set-specific blocks ─────────────────────────────────────────────

def _block_payment_init(con: duckdb.DuckDBPyConnection, ms: str) -> str:
    v = f"detail_{ms}"

    ccy_df = _run_query(con, f"""
        SELECT COALESCE(currency,'(none)') AS currency,
               COUNT(*) AS transactions,
               ROUND(SUM(amount) FILTER (WHERE amount BETWEEN 0 AND 10000000), 2) AS total_value,
               ROUND(AVG(amount) FILTER (WHERE amount BETWEEN 0 AND 10000000), 2) AS avg_amount
        FROM {v} WHERE currency IS NOT NULL
        GROUP BY currency ORDER BY transactions DESC LIMIT 12
    """)

    bkt_df = _run_query(con, f"""
        SELECT CASE
                   WHEN amount < 1000        THEN '&lt; 1,000'
                   WHEN amount < 10000       THEN '1k – 9,999'
                   WHEN amount < 100000      THEN '10k – 99,999'
                   WHEN amount < 1000000     THEN '100k – 999,999'
                   ELSE                           '1M – 10M'
               END AS bucket,
               COUNT(*) AS count,
               ROUND(AVG(amount), 2) AS avg_amount
        FROM {v} WHERE amount BETWEEN 0 AND 10000000
        GROUP BY bucket ORDER BY MIN(amount)
    """)

    corr_df = _run_query(con, f"""
        SELECT COALESCE(debtor_pfx,'??')   AS debtor,
               COALESCE(creditor_pfx,'??') AS creditor,
               COUNT(*) AS count,
               CASE WHEN debtor_pfx IS NOT DISTINCT FROM creditor_pfx
                    THEN 'domestic' ELSE 'cross-border' END AS flow
        FROM {v}
        WHERE debtor_pfx IS NOT NULL AND creditor_pfx IS NOT NULL
        GROUP BY debtor, creditor, flow ORDER BY count DESC LIMIT 10
    """)

    chg_html = ""
    if ms == "001":
        chg_df  = _run_query(con, f"""
            SELECT COALESCE(charge_bearer,'(absent)') AS charge_bearer,
                   COUNT(*) AS count,
                   ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
            FROM {v} GROUP BY charge_bearer ORDER BY count DESC
        """)
        chg_html = f'<div class="sub"><h3>Charge Bearer (ChrgBr)</h3>{_table(chg_df)}</div>'

    nd = '<p class="nd">No data.</p>'
    nd_iban = '<p class="nd">No IBAN data.</p>'
    return (
        '<div class="g2">'
        f'<div class="sub"><h3>Currency Distribution</h3>{_table(ccy_df) if not ccy_df.empty else nd}</div>'
        f'<div class="sub"><h3>Amount Buckets (0 – 10 M)</h3>{_table(bkt_df) if not bkt_df.empty else nd}</div>'
        "</div>"
        f'<div class="sub"><h3>IBAN Corridors (Debtor Country &rarr; Creditor Country)</h3>{_table(corr_df) if not corr_df.empty else nd_iban}</div>'
        + chg_html
    )


def _block_status_report(con: duckdb.DuckDBPyConnection, ms: str) -> str:
    v = f"detail_{ms}"

    grp_df = _run_query(con, f"""
        SELECT COALESCE(grp_status,'(none)') AS group_status, COUNT(*) AS count
        FROM {v} WHERE grp_status IS NOT NULL
        GROUP BY grp_status ORDER BY count DESC
    """)

    tx_df = _run_query(con, f"""
        SELECT COALESCE(tx_status,'(none)') AS tx_status, COUNT(*) AS count
        FROM {v} WHERE tx_status IS NOT NULL
        GROUP BY tx_status ORDER BY count DESC
    """)

    rsn_df = _run_query(con, f"""
        SELECT COALESCE(reason_code,'(none)') AS reason_code, COUNT(*) AS count
        FROM {v} WHERE reason_code IS NOT NULL
        GROUP BY reason_code ORDER BY count DESC LIMIT 10
    """)

    status_colors: dict[str, str] = {
        "ACCP": "var(--green)", "RJCT": "var(--coral)",
        "PDNG": "var(--amber)", "ACSP": "var(--teal)", "PART": "var(--plum)",
    }
    bar_rows: list[tuple[str, int | float, int | float, str]] = []
    if not grp_df.empty:
        max_c = int(grp_df["count"].max())
        for _, r in grp_df.iterrows():
            sc = status_colors.get(str(r["group_status"]), "var(--teal)")
            bar_rows.append((str(r["group_status"]), int(r["count"]), max_c, sc))

    grp_html = _hbars(bar_rows) if bar_rows else '<p class="nd">No group status data.</p>'
    tx_html  = _table(tx_df)    if not tx_df.empty  else '<p class="nd">No transaction status data.</p>'
    rsn_html = _table(rsn_df)   if not rsn_df.empty else '<p class="nd">No reason codes.</p>'

    return (
        '<div class="g2">'
        f'<div class="sub"><h3>Group Status Distribution</h3>{grp_html}</div>'
        '<div style="display:flex;flex-direction:column;gap:0.85rem;">'
        f'<div class="sub"><h3>Transaction Status Codes</h3>{tx_html}</div>'
        f'<div class="sub"><h3>Status Reason Codes</h3>{rsn_html}</div>'
        "</div></div>"
    )


def _block_reversal(con: duckdb.DuckDBPyConnection) -> str:
    rsn_df = _run_query(con, """
        SELECT COALESCE(reversal_reason,'(none)') AS reason, COUNT(*) AS count,
               ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(),1) AS pct
        FROM detail_007 WHERE reversal_reason IS NOT NULL
        GROUP BY reason ORDER BY count DESC
    """)

    ccy_df = _run_query(con, """
        SELECT COALESCE(currency,'(none)') AS currency, COUNT(*) AS count,
               ROUND(SUM(orig_amount) FILTER (WHERE orig_amount BETWEEN 0 AND 10000000),2) AS total
        FROM detail_007 WHERE currency IS NOT NULL
        GROUP BY currency ORDER BY count DESC
    """)

    bkt_df = _run_query(con, """
        SELECT CASE
                   WHEN orig_amount < 1000    THEN '&lt; 1,000'
                   WHEN orig_amount < 10000   THEN '1k – 9,999'
                   WHEN orig_amount < 100000  THEN '10k – 99,999'
                   WHEN orig_amount < 1000000 THEN '100k – 999,999'
                   ELSE                            '1M – 10M' END AS bucket,
               COUNT(*) AS count
        FROM detail_007 WHERE orig_amount BETWEEN 0 AND 10000000
        GROUP BY bucket ORDER BY MIN(orig_amount)
    """)

    reason_colors: dict[str, str] = {
        "DUPL": "var(--coral)", "FRAD": "var(--coral)",
        "TECH": "var(--amber)", "CUST": "var(--teal)",
        "AC04": "var(--amber)", "UPAY": "var(--plum)",
    }
    bar_rows: list[tuple[str, int | float, int | float, str]] = []
    if not rsn_df.empty:
        max_c = int(rsn_df["count"].max())
        for _, r in rsn_df.iterrows():
            rc = str(r["reason"])
            bar_rows.append((rc, int(r["count"]), max_c, reason_colors.get(rc, "var(--teal)")))

    nd = '<p class="nd">No data.</p>'
    rsn_html = _hbars(bar_rows) if bar_rows else '<p class="nd">No reason code data.</p>'
    return (
        '<div class="g2">'
        f'<div class="sub"><h3>Reversal Reason Codes</h3>{rsn_html}</div>'
        '<div style="display:flex;flex-direction:column;gap:0.85rem;">'
        f'<div class="sub"><h3>Original Amount Buckets</h3>{_table(bkt_df) if not bkt_df.empty else nd}</div>'
        f'<div class="sub"><h3>Original Currency Breakdown</h3>{_table(ccy_df) if not ccy_df.empty else nd}</div>'
        "</div></div>"
    )


def _block_mandate_init(con: duckdb.DuckDBPyConnection) -> str:
    trk_df = _run_query(con, """
        SELECT COALESCE(tracking_ind,'(absent)') AS tracking, COUNT(*) AS count,
               ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(),1) AS pct
        FROM detail_009 GROUP BY tracking ORDER BY count DESC
    """)
    pfx_df = _run_query(con, """
        SELECT COALESCE(creditor_pfx,'??') AS creditor_country, COUNT(*) AS count
        FROM detail_009 WHERE creditor_pfx IS NOT NULL
        GROUP BY creditor_country ORDER BY count DESC LIMIT 10
    """)
    trk_html = _table(trk_df) if not trk_df.empty else '<p class="nd">No data.</p>'
    pfx_html = _table(pfx_df) if not pfx_df.empty else '<p class="nd">No IBAN data.</p>'
    return (
        '<div class="g2">'
        f'<div class="sub"><h3>Tracking Indicator (TrckgInd)</h3>{trk_html}</div>'
        f'<div class="sub"><h3>Creditor Country (from IBAN)</h3>{pfx_html}</div>'
        "</div>"
    )


def _block_reason_codes(con: duckdb.DuckDBPyConnection, ms: str, col: str, title: str) -> str:
    df = _run_query(con, f"""
        SELECT COALESCE({col},'(none)') AS reason_code, COUNT(*) AS count,
               ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER(),1) AS pct
        FROM detail_{ms} WHERE {col} IS NOT NULL
        GROUP BY reason_code ORDER BY count DESC
    """)
    body = _table(df) if not df.empty else '<p class="nd">No reason codes found.</p>'
    return f'<div class="sub"><h3>{_esc(title)}</h3>{body}</div>'


def _block_012(con: duckdb.DuckDBPyConnection) -> str:
    acc_df = _run_query(con, """
        SELECT COALESCE(accepted,'(absent)') AS accepted, COUNT(*) AS count
        FROM detail_012 GROUP BY accepted ORDER BY count DESC
    """)
    rsn_df = _run_query(con, """
        SELECT COALESCE(rejection_reason,'(none)') AS rejection_reason, COUNT(*) AS count
        FROM detail_012 WHERE rejection_reason IS NOT NULL
        GROUP BY rejection_reason ORDER BY count DESC
    """)

    accepted_n = 0
    rejected_n = 0
    if not acc_df.empty:
        for _, r in acc_df.iterrows():
            v = str(r["accepted"]).lower()
            if v == "true":
                accepted_n = int(r["count"])
            elif v == "false":
                rejected_n = int(r["count"])
    total_a = accepted_n + rejected_n or 1
    pp = round(100 * accepted_n / total_a)
    fp = round(100 * rejected_n / total_a)

    bar_html = (
        f'<div style="display:flex;height:24px;border-radius:6px;overflow:hidden;margin-bottom:0.6rem;">'
        f'<div style="width:{pp}%;background:var(--green);display:flex;align-items:center;'
        f'padding:0 8px;color:#fff;font-size:0.7rem;font-weight:700;">'
        f'{"Accepted " + str(accepted_n) if pp >= 15 else ""}</div>'
        f'<div style="width:{fp}%;background:var(--coral);display:flex;align-items:center;'
        f'padding:0 8px;color:#fff;font-size:0.7rem;font-weight:700;">'
        f'{"Rejected " + str(rejected_n) if fp >= 15 else ""}</div>'
        f'</div>'
    )

    rsn_html = _table(rsn_df) if not rsn_df.empty else '<p class="nd">No rejection reasons.</p>'
    return (
        '<div class="g2">'
        f'<div class="sub"><h3>Acceptance Result</h3>{bar_html}{_table(acc_df, "accepted")}</div>'
        f'<div class="sub"><h3>Rejection Reason Codes</h3>{rsn_html}</div>'
        "</div>"
    )


def _block_017(con: duckdb.DuckDBPyConnection) -> str:
    df = _run_query(con, "SELECT COUNT(DISTINCT orig_mandate_id) AS unique_mandate_ids FROM detail_017")
    n  = int(df.iloc[0]["unique_mandate_ids"]) if not df.empty else 0
    return (
        f'<div class="sub"><h3>Mandate Copy Requests</h3>'
        f'<p style="font-size:0.82rem;color:#3a4a52;">'
        f'{n} unique original mandate IDs referenced across all copy-request files.</p></div>'
    )


# ── Section builder ────────────────────────────────────────────────────────

def _html_section(con: duckdb.DuckDBPyConnection, ms: str, bdf: pd.DataFrame) -> str:
    schema = _schema_version(ms)
    label  = _MS_LABELS[ms]
    color  = _MS_COLOR.get(ms, "teal")

    header = (
        f'<div class="sh {color}">'
        f'<h2>pain.{ms}</h2>'
        f'<span class="schema-badge">{_esc(schema)}</span>'
        f'<span class="ms-lbl">{_esc(label)}</span>'
        f'</div>'
    )

    stat_html = _ms_stat_block(con, ms, bdf)
    cat_html  = f'<div class="sub"><h3>File Category Breakdown</h3>{_cat_cards(bdf)}</div>'
    err_html  = _error_block(con, ms)

    if ms in ("001", "008", "013"):
        specific = _block_payment_init(con, ms)
    elif ms in ("002", "014"):
        specific = _block_status_report(con, ms)
    elif ms == "007":
        specific = _block_reversal(con)
    elif ms == "009":
        specific = _block_mandate_init(con)
    elif ms == "010":
        specific = _block_reason_codes(con, "010", "amendment_reason", "Amendment Reason Codes (AM-codes)")
    elif ms == "011":
        specific = _block_reason_codes(con, "011", "cancellation_reason", "Cancellation Reason Codes")
    elif ms == "012":
        specific = _block_012(con)
    elif ms == "017":
        specific = _block_017(con)
    elif ms == "018":
        specific = _block_reason_codes(con, "018", "suspension_reason", "Suspension Reason Codes")
    else:
        specific = ""

    return (
        f'<section class="sec" id="s{ms}">'
        + header
        + stat_html
        + cat_html
        + err_html
        + specific
        + "</section>"
    )


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _REPORTS.mkdir(exist_ok=True)
    report_path = _REPORTS / f"analytics_report_{ts}.html"

    con = duckdb.connect()
    con.execute(f"ATTACH '{_STATE_DB}' AS state (TYPE sqlite, READ_ONLY)")

    dfs: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}

    print("Parsing XML files:")
    for ms in _MESSAGE_SETS:
        print(f"  pain.{ms}...", end=" ", flush=True)
        bdf, ddf = _load_ms(ms)
        dfs[ms] = (bdf, ddf)
        if not bdf.empty:
            con.register(f"base_{ms}", bdf)
        if not ddf.empty:
            con.register(f"detail_{ms}", ddf)
        print(f"{len(bdf)} files, {len(ddf)} records")

    print("\nGenerating report sections:")
    parts = [_html_head(), _html_sidebar(), _html_page_header(ts), _html_overview(con)]

    for ms in _MESSAGE_SETS:
        print(f"  pain.{ms}...", flush=True)
        bdf = dfs[ms][0]
        parts.append(_html_section(con, ms, bdf))

    parts.append(_html_footer())

    report_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    main()
