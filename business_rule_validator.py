"""ISO 20022 business rule validator — semantic checks beyond XSD structure."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from lxml import etree


@dataclass(frozen=True)
class RuleViolation:
    rule_id: str
    severity: str        # "ERROR" | "WARNING"
    field_path: str
    message: str
    actual: str | None = None
    expected: str | None = None


_Rule = Callable[[etree._Element, str], list[RuleViolation]]

# ── ISO 4217 active currency codes ────────────────────────────────────────────
_ISO4217: frozenset[str] = frozenset({
    "AED","AFN","ALL","AMD","ANG","AOA","ARS","AUD","AWG","AZN",
    "BAM","BBD","BDT","BGN","BHD","BIF","BMD","BND","BOB","BRL",
    "BSD","BTN","BWP","BYN","BZD","CAD","CDF","CHF","CLP","CNY",
    "COP","CRC","CUP","CVE","CZK","DJF","DKK","DOP","DZD","EGP",
    "ERN","ETB","EUR","FJD","FKP","GBP","GEL","GHS","GIP","GMD",
    "GNF","GTQ","GYD","HKD","HNL","HTG","HUF","IDR","ILS","INR",
    "IQD","IRR","ISK","JMD","JOD","JPY","KES","KGS","KHR","KMF",
    "KRW","KWD","KYD","KZT","LAK","LBP","LKR","LRD","LSL","LYD",
    "MAD","MDL","MGA","MKD","MMK","MNT","MOP","MUR","MVR","MWK",
    "MXN","MYR","MZN","NAD","NGN","NIO","NOK","NPR","NZD","OMR",
    "PAB","PEN","PGK","PHP","PKR","PLN","PYG","QAR","RON","RSD",
    "RUB","RWF","SAR","SBD","SCR","SDG","SEK","SGD","SHP","SOS",
    "SRD","SSP","STN","SZL","THB","TJS","TMT","TND","TOP","TRY",
    "TTD","TWD","TZS","UAH","UGX","USD","UYU","UZS","VES","VND",
    "VUV","WST","XAF","XCD","XOF","XPF","YER","ZAR","ZMW","ZWL",
})

# ── Valid status / reason code sets ──────────────────────────────────────────
_GRP_STATUS_CODES:  frozenset[str] = frozenset({"ACCP","ACSP","RJCT","PDNG","PART","RCVD","ACTC","ACWC"})
_TX_STATUS_CODES:   frozenset[str] = frozenset({"ACCP","ACSP","RJCT","PDNG","ACWC","ACTC","CANC"})
_RVSL_REASON_CODES: frozenset[str] = frozenset({"DUPL","FRAD","TECH","CUST","UPAY","AC04","AM09","NOAS","NARR"})
_CXL_REASON_CODES:  frozenset[str] = frozenset({"CUST","AGNT","UPAY","DUPL","FRAD","AM09","NARR"})
_AMD_REASON_CODES:  frozenset[str] = frozenset({"AM05","AM06","AM09","MD01","MD02","MD06","MD07","NARR"})
_SSPN_REASON_CODES: frozenset[str] = frozenset({"SSPD","UPAY","CUST","NARR"})
_RJCT_REASON_CODES: frozenset[str] = frozenset({
    "AC01","AC04","AC06","AC13","AG01","AG02","AM01","AM02","AM03","AM04",
    "AM05","AM06","AM07","AM09","AM10","BE01","BE04","BE05","CH03","CH16",
    "CUST","DT01","ED01","ED03","EMVL","FF01","FF05","FOCR","FR01","MD01",
    "MD02","MD06","MD07","MS02","MS03","NARR","RC01","RF01","RR01","RR02",
    "RR03","RR04","SL01","SL02","SL11","SL12","SL13","SL14","UPAY",
})

_BIC_RE   = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _xp(root: etree._Element, path: str, ns: str) -> list[etree._Element]:
    return root.xpath(path, namespaces={"p": ns})


def _txt(root: etree._Element, path: str, ns: str) -> str | None:
    els = _xp(root, path, ns)
    if els and els[0].text:
        return els[0].text.strip()
    return None


def _validate_iban(iban: str) -> bool:
    iban = iban.replace(" ", "").upper()
    if len(iban) < 5 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - ord("A") + 10) if c.isalpha() else c for c in rearranged)
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


def _validate_bic(bic: str) -> bool:
    return bool(_BIC_RE.match(bic.upper()))


def _validate_uuid4(value: str) -> bool:
    return bool(_UUID4_RE.match(value.strip()))


def _today() -> date:
    return datetime.utcnow().date()


def _violation(rule_id: str, severity: str, field: str, msg: str,
               actual: str | None = None, expected: str | None = None) -> RuleViolation:
    return RuleViolation(rule_id=rule_id, severity=severity, field_path=field,
                         message=msg, actual=actual, expected=expected)


# ── Shared sub-checks used across multiple message sets ───────────────────────

def _check_ibans(root: etree._Element, ns: str, xpath: str,
                 rule_id: str, field_label: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, xpath, ns):
        val = el.text.strip() if el.text else ""
        if val and not _validate_iban(val):
            violations.append(_violation(
                rule_id, "ERROR", field_label,
                f"IBAN failed ISO 7064 MOD-97 check digit validation",
                actual=val,
            ))
    return violations


def _check_bics(root: etree._Element, ns: str, xpath: str,
                rule_id: str, field_label: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, xpath, ns):
        val = el.text.strip() if el.text else ""
        if val and not _validate_bic(val):
            violations.append(_violation(
                rule_id, "ERROR", field_label,
                "BIC must be 8 or 11 uppercase alphanumeric characters (AAAA BB CC [DDD])",
                actual=val,
            ))
    return violations


def _check_currencies(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in root.iter():
        ccy = el.get("Ccy")
        if ccy and ccy.upper() not in _ISO4217:
            violations.append(_violation(
                "BR-010", "ERROR", el.tag.split("}")[-1] + "/@Ccy",
                "Currency code is not a valid ISO 4217 active currency code",
                actual=ccy,
            ))
    return violations


# ── pain.001 / pain.013 — Credit Transfer ────────────────────────────────────

def _rules_credit_transfer(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-001: GrpHdr/NbOfTxs == actual count of CdtTrfTxInf
    nb_txs_str = _txt(root, "//p:GrpHdr/p:NbOfTxs", ns)
    actual_tx_count = len(_xp(root, "//p:CdtTrfTxInf", ns))
    if nb_txs_str is not None:
        try:
            declared = int(nb_txs_str)
            if declared != actual_tx_count:
                v.append(_violation(
                    "BR-001", "ERROR", "GrpHdr/NbOfTxs",
                    f"Declared NbOfTxs ({declared}) does not match actual transaction count ({actual_tx_count})",
                    actual=str(actual_tx_count), expected=str(declared),
                ))
        except ValueError:
            v.append(_violation("BR-001", "ERROR", "GrpHdr/NbOfTxs",
                                 "NbOfTxs is not a valid integer", actual=nb_txs_str))

    # BR-002: GrpHdr/CtrlSum == sum(InstdAmt) ± 0.01
    ctrl_sum_str = _txt(root, "//p:GrpHdr/p:CtrlSum", ns)
    if ctrl_sum_str is not None:
        try:
            declared_sum = float(ctrl_sum_str)
            amounts = []
            for el in _xp(root, "//p:CdtTrfTxInf/p:Amt/p:InstdAmt", ns):
                if el.text:
                    try:
                        amounts.append(float(el.text.strip()))
                    except ValueError:
                        pass
            if amounts:
                actual_sum = round(sum(amounts), 5)
                if abs(declared_sum - actual_sum) > 0.01:
                    v.append(_violation(
                        "BR-002", "ERROR", "GrpHdr/CtrlSum",
                        f"CtrlSum ({declared_sum}) does not match sum of InstdAmt ({actual_sum})",
                        actual=str(actual_sum), expected=str(declared_sum),
                    ))
        except ValueError:
            v.append(_violation("BR-002", "ERROR", "GrpHdr/CtrlSum",
                                 "CtrlSum is not a valid decimal", actual=ctrl_sum_str))

    # BR-003/BR-004: Per-PmtInf NbOfTxs and CtrlSum checks
    for i, pmt in enumerate(_xp(root, "//p:PmtInf", ns), start=1):
        pmt_txs = _xp(pmt, "p:CdtTrfTxInf", ns)
        pmt_nb  = _txt(pmt, "p:NbOfTxs", ns)
        if pmt_nb is not None:
            try:
                if int(pmt_nb) != len(pmt_txs):
                    v.append(_violation(
                        "BR-004", "ERROR", f"PmtInf[{i}]/NbOfTxs",
                        f"PmtInf NbOfTxs ({pmt_nb}) != transaction count ({len(pmt_txs)})",
                        actual=str(len(pmt_txs)), expected=pmt_nb,
                    ))
            except ValueError:
                pass
        pmt_ctrl = _txt(pmt, "p:CtrlSum", ns)
        if pmt_ctrl is not None:
            try:
                pmt_amts = []
                for el in _xp(pmt, "p:CdtTrfTxInf/p:Amt/p:InstdAmt", ns):
                    if el.text:
                        try:
                            pmt_amts.append(float(el.text.strip()))
                        except ValueError:
                            pass
                if pmt_amts:
                    actual_pmt_sum = round(sum(pmt_amts), 5)
                    if abs(float(pmt_ctrl) - actual_pmt_sum) > 0.01:
                        v.append(_violation(
                            "BR-003", "ERROR", f"PmtInf[{i}]/CtrlSum",
                            f"PmtInf CtrlSum ({pmt_ctrl}) != sum of InstdAmt ({actual_pmt_sum})",
                            actual=str(actual_pmt_sum), expected=pmt_ctrl,
                        ))
            except ValueError:
                pass

    # BR-005: EndToEndId unique within the message
    e2e_ids: list[str] = []
    seen_e2e: set[str] = set()
    for el in _xp(root, "//p:CdtTrfTxInf/p:PmtId/p:EndToEndId", ns):
        val = el.text.strip() if el.text else ""
        if val in seen_e2e:
            v.append(_violation(
                "BR-005", "ERROR", "CdtTrfTxInf/PmtId/EndToEndId",
                f"Duplicate EndToEndId within the message", actual=val,
            ))
        else:
            seen_e2e.add(val)
        e2e_ids.append(val)

    # BR-006: UETR must be UUID v4 (if present)
    for el in _xp(root, "//p:CdtTrfTxInf/p:PmtId/p:UETR", ns):
        val = el.text.strip() if el.text else ""
        if val and not _validate_uuid4(val):
            v.append(_violation(
                "BR-006", "ERROR", "CdtTrfTxInf/PmtId/UETR",
                "UETR must be a valid UUID v4 (third group starts with '4', "
                "fourth group starts with 8/9/a/b)",
                actual=val,
            ))

    # BR-007: InstdAmt > 0
    for el in _xp(root, "//p:CdtTrfTxInf/p:Amt/p:InstdAmt", ns):
        try:
            amt = float(el.text.strip()) if el.text else 0.0
            if amt <= 0:
                v.append(_violation(
                    "BR-007", "ERROR", "CdtTrfTxInf/Amt/InstdAmt",
                    "InstdAmt must be greater than zero",
                    actual=el.text.strip() if el.text else "0",
                ))
        except ValueError:
            pass

    # BR-008: IBAN check digit validation
    v.extend(_check_ibans(root, ns, "//p:DbtrAcct/p:Id/p:IBAN",  "BR-008", "DbtrAcct/Id/IBAN"))
    v.extend(_check_ibans(root, ns, "//p:CdtrAcct/p:Id/p:IBAN",  "BR-008", "CdtrAcct/Id/IBAN"))

    # BR-009: BIC format validation
    v.extend(_check_bics(root, ns, "//p:DbtrAgt/p:FinInstnId/p:BICFI",  "BR-009", "DbtrAgt/FinInstnId/BICFI"))
    v.extend(_check_bics(root, ns, "//p:CdtrAgt/p:FinInstnId/p:BICFI",  "BR-009", "CdtrAgt/FinInstnId/BICFI"))
    v.extend(_check_bics(root, ns, "//p:IntrmyAgt1/p:FinInstnId/p:BICFI","BR-009", "IntrmyAgt1/FinInstnId/BICFI"))

    # BR-010: Currency code is valid ISO 4217
    v.extend(_check_currencies(root, ns))

    # BR-011: ReqdExctnDt not in the past (WARNING)
    for el in _xp(root, "//p:ReqdExctnDt/p:Dt", ns):
        try:
            req_date = date.fromisoformat(el.text.strip())
            if req_date < _today():
                v.append(_violation(
                    "BR-011", "WARNING", "PmtInf/ReqdExctnDt/Dt",
                    "Requested execution date is in the past",
                    actual=el.text.strip(), expected=f">= {_today().isoformat()}",
                ))
        except (ValueError, AttributeError):
            pass

    # BR-012: MsgId must be non-empty
    msg_id = _txt(root, "//p:GrpHdr/p:MsgId", ns)
    if not msg_id:
        v.append(_violation("BR-012", "ERROR", "GrpHdr/MsgId",
                             "MsgId is missing or empty"))

    return v


# ── pain.002 / pain.014 — Payment Status Report ───────────────────────────────

def _rules_status_report(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-020: GrpSts must be a valid code
    for el in _xp(root, "//p:OrgnlGrpInfAndSts/p:GrpSts", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _GRP_STATUS_CODES:
            v.append(_violation(
                "BR-020", "ERROR", "OrgnlGrpInfAndSts/GrpSts",
                f"GrpSts '{val}' is not a recognised ISO 20022 group status code",
                actual=val, expected=f"one of {sorted(_GRP_STATUS_CODES)}",
            ))

    # BR-021: TxSts must be a valid code
    for el in _xp(root, "//p:TxInfAndSts/p:TxSts", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _TX_STATUS_CODES:
            v.append(_violation(
                "BR-021", "ERROR", "TxInfAndSts/TxSts",
                f"TxSts '{val}' is not a recognised ISO 20022 transaction status code",
                actual=val,
            ))

    # BR-022: Reason codes (WARNING — external code list is non-exhaustive)
    for el in _xp(root, "//p:StsRsnInf/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _RJCT_REASON_CODES:
            v.append(_violation(
                "BR-022", "WARNING", "StsRsnInf/Rsn/Cd",
                f"Status reason code '{val}' is not in the common ISO 20022 reason code set",
                actual=val,
            ))

    # BR-023: OrgnlMsgId must be present
    for block in _xp(root, "//p:OrgnlGrpInfAndSts", ns):
        orig_id = _txt(block, "p:OrgnlMsgId", ns)
        if not orig_id:
            v.append(_violation("BR-023", "ERROR", "OrgnlGrpInfAndSts/OrgnlMsgId",
                                 "Original message reference OrgnlMsgId is missing or empty"))

    return v


# ── pain.007 — Customer Payment Reversal ─────────────────────────────────────

def _rules_reversal(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-030: Each TxInf must have OrgnlEndToEndId
    for i, tx in enumerate(_xp(root, "//p:TxInf", ns), start=1):
        e2e = _txt(tx, "p:OrgnlEndToEndId", ns)
        if not e2e:
            v.append(_violation(
                "BR-030", "ERROR", f"OrgnlPmtInfAndRvsl/TxInf[{i}]/OrgnlEndToEndId",
                "OrgnlEndToEndId is required for each reversal transaction — "
                "it must match the EndToEndId from the original pain.001",
            ))

    # BR-031: Reversal reason codes (WARNING)
    for el in _xp(root, "//p:RvslRsnInf/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _RVSL_REASON_CODES:
            v.append(_violation(
                "BR-031", "WARNING", "RvslRsnInf/Rsn/Cd",
                f"Reversal reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_RVSL_REASON_CODES)}",
            ))

    # BR-032: OrgnlInstdAmt > 0
    for el in _xp(root, "//p:OrgnlInstdAmt", ns):
        try:
            if el.text and float(el.text.strip()) <= 0:
                v.append(_violation(
                    "BR-032", "ERROR", "TxInf/OrgnlInstdAmt",
                    "OrgnlInstdAmt must be greater than zero",
                    actual=el.text.strip(),
                ))
        except ValueError:
            pass

    # BR-010: Currency codes
    v.extend(_check_currencies(root, ns))

    return v


# ── pain.008 — Customer Direct Debit Initiation ───────────────────────────────

def _rules_direct_debit(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-040: GrpHdr/NbOfTxs == count(DrctDbtTxInf)
    nb_txs_str = _txt(root, "//p:GrpHdr/p:NbOfTxs", ns)
    actual_count = len(_xp(root, "//p:DrctDbtTxInf", ns))
    if nb_txs_str is not None:
        try:
            declared = int(nb_txs_str)
            if declared != actual_count:
                v.append(_violation(
                    "BR-040", "ERROR", "GrpHdr/NbOfTxs",
                    f"Declared NbOfTxs ({declared}) != actual DrctDbtTxInf count ({actual_count})",
                    actual=str(actual_count), expected=str(declared),
                ))
        except ValueError:
            pass

    # BR-041: GrpHdr/CtrlSum == sum(InstdAmt)
    ctrl_sum_str = _txt(root, "//p:GrpHdr/p:CtrlSum", ns)
    if ctrl_sum_str is not None:
        try:
            declared_sum = float(ctrl_sum_str)
            amounts = [
                float(el.text.strip())
                for el in _xp(root, "//p:DrctDbtTxInf/p:InstdAmt", ns)
                if el.text
            ]
            if amounts:
                actual_sum = round(sum(amounts), 5)
                if abs(declared_sum - actual_sum) > 0.01:
                    v.append(_violation(
                        "BR-041", "ERROR", "GrpHdr/CtrlSum",
                        f"CtrlSum ({declared_sum}) != sum of InstdAmt ({actual_sum})",
                        actual=str(actual_sum), expected=str(declared_sum),
                    ))
        except ValueError:
            pass

    # BR-042: MndtId must be present in each DrctDbtTxInf
    for i, tx in enumerate(_xp(root, "//p:DrctDbtTxInf", ns), start=1):
        mndt_id = _txt(tx, "p:MndtRltdInf/p:MndtId", ns)
        if not mndt_id:
            v.append(_violation(
                "BR-042", "ERROR", f"DrctDbtTxInf[{i}]/MndtRltdInf/MndtId",
                "MndtId is required — each direct debit transaction must reference a mandate",
            ))

    # BR-043: IBAN validation
    v.extend(_check_ibans(root, ns, "//p:CdtrAcct/p:Id/p:IBAN", "BR-043", "CdtrAcct/Id/IBAN"))
    v.extend(_check_ibans(root, ns, "//p:DbtrAcct/p:Id/p:IBAN", "BR-043", "DbtrAcct/Id/IBAN"))

    # BR-044: BIC validation
    v.extend(_check_bics(root, ns, "//p:CdtrAgt/p:FinInstnId/p:BICFI", "BR-044", "CdtrAgt/FinInstnId/BICFI"))
    v.extend(_check_bics(root, ns, "//p:DbtrAgt/p:FinInstnId/p:BICFI", "BR-044", "DbtrAgt/FinInstnId/BICFI"))

    # BR-045: Currency codes
    v.extend(_check_currencies(root, ns))

    # BR-046: ReqdColltnDt not in past (WARNING)
    for el in _xp(root, "//p:ReqdColltnDt", ns):
        try:
            req_date = date.fromisoformat(el.text.strip())
            if req_date < _today():
                v.append(_violation(
                    "BR-046", "WARNING", "PmtInf/ReqdColltnDt",
                    "Requested collection date is in the past",
                    actual=el.text.strip(), expected=f">= {_today().isoformat()}",
                ))
        except (ValueError, AttributeError):
            pass

    return v


# ── pain.009 — Mandate Initiation ────────────────────────────────────────────

def _rules_mandate_initiation(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-050: MndtReqId must not be empty
    for el in _xp(root, "//p:MndtReqId", ns):
        if not el.text or not el.text.strip():
            v.append(_violation("BR-050", "ERROR", "Mndt/MndtReqId",
                                 "MndtReqId is required and must not be empty"))

    # BR-051: TrckgInd must be true or false
    for el in _xp(root, "//p:TrckgInd", ns):
        val = el.text.strip().lower() if el.text else ""
        if val not in ("true", "false", "1", "0"):
            v.append(_violation(
                "BR-051", "ERROR", "Mndt/TrckgInd",
                "TrckgInd must be 'true' or 'false'", actual=el.text,
            ))

    # BR-052/053: IBAN and BIC
    v.extend(_check_ibans(root, ns, "//p:CdtrAcct/p:Id/p:IBAN", "BR-052", "CdtrAcct/Id/IBAN"))
    v.extend(_check_ibans(root, ns, "//p:DbtrAcct/p:Id/p:IBAN", "BR-052", "DbtrAcct/Id/IBAN"))
    v.extend(_check_bics(root, ns, "//p:CdtrAgt/p:FinInstnId/p:BICFI", "BR-053", "CdtrAgt/FinInstnId/BICFI"))
    v.extend(_check_bics(root, ns, "//p:DbtrAgt/p:FinInstnId/p:BICFI", "BR-053", "DbtrAgt/FinInstnId/BICFI"))

    return v


# ── pain.010 — Mandate Amendment ─────────────────────────────────────────────

def _rules_mandate_amendment(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-060: Amendment reason code (WARNING)
    for el in _xp(root, "//p:AmdmntRsn/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _AMD_REASON_CODES:
            v.append(_violation(
                "BR-060", "WARNING", "AmdmntRsn/Rsn/Cd",
                f"Amendment reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_AMD_REASON_CODES)}",
            ))

    # BR-061: MndtId must not be empty
    for el in _xp(root, "//p:Mndt/p:MndtId", ns):
        if not el.text or not el.text.strip():
            v.append(_violation("BR-061", "ERROR", "UndrlygAmdmntDtls/Mndt/MndtId",
                                 "MndtId is required in mandate amendment"))

    return v


# ── pain.011 — Mandate Cancellation ──────────────────────────────────────────

def _rules_mandate_cancellation(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-070: Cancellation reason code (WARNING)
    for el in _xp(root, "//p:CxlRsn/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _CXL_REASON_CODES:
            v.append(_violation(
                "BR-070", "WARNING", "CxlRsn/Rsn/Cd",
                f"Cancellation reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_CXL_REASON_CODES)}",
            ))

    # BR-071: OrgnlMndtId must not be empty
    for el in _xp(root, "//p:OrgnlMndt/p:OrgnlMndtId", ns):
        if not el.text or not el.text.strip():
            v.append(_violation("BR-071", "ERROR", "UndrlygCxlDtls/OrgnlMndt/OrgnlMndtId",
                                 "Original mandate ID is required in cancellation request"))

    return v


# ── pain.012 — Mandate Acceptance Report ─────────────────────────────────────

def _rules_mandate_acceptance(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-080: Accptd must be true or false
    for el in _xp(root, "//p:AccptncRslt/p:Accptd", ns):
        val = el.text.strip().lower() if el.text else ""
        if val not in ("true", "false", "1", "0"):
            v.append(_violation(
                "BR-080", "ERROR", "UndrlygAccptncDtls/AccptncRslt/Accptd",
                "Accptd must be 'true' or 'false'", actual=el.text,
            ))

    # BR-081: If rejected, reason code should be present (WARNING)
    for block in _xp(root, "//p:UndrlygAccptncDtls", ns):
        accptd = _txt(block, "p:AccptncRslt/p:Accptd", ns)
        if accptd and accptd.lower() in ("false", "0"):
            rsn = _txt(block, "p:AccptncRslt/p:RjctRsn/p:Rsn/p:Cd", ns)
            if not rsn:
                v.append(_violation(
                    "BR-081", "WARNING", "AccptncRslt/RjctRsn/Rsn/Cd",
                    "Rejection reason code is recommended when Accptd=false",
                ))

    return v


# ── pain.017 — Mandate Copy Request ──────────────────────────────────────────

def _rules_mandate_copy(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-090: OrgnlMndtId must not be empty
    for el in _xp(root, "//p:OrgnlMndt/p:OrgnlMndtId", ns):
        if not el.text or not el.text.strip():
            v.append(_violation("BR-090", "ERROR", "UndrlygCpyReqDtls/OrgnlMndt/OrgnlMndtId",
                                 "Original mandate ID is required in copy request"))

    return v


# ── pain.018 — Mandate Suspension ────────────────────────────────────────────

def _rules_mandate_suspension(root: etree._Element, ns: str) -> list[RuleViolation]:
    v: list[RuleViolation] = []

    # BR-100: Suspension reason code (WARNING)
    for el in _xp(root, "//p:SspnsnRsn/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _SSPN_REASON_CODES:
            v.append(_violation(
                "BR-100", "WARNING", "SspnsnRsn/Rsn/Cd",
                f"Suspension reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_SSPN_REASON_CODES)}",
            ))

    # BR-101: OrgnlMndtId must not be empty
    for el in _xp(root, "//p:OrgnlMndt/p:OrgnlMndtId", ns):
        if not el.text or not el.text.strip():
            v.append(_violation("BR-101", "ERROR", "UndrlygSspnsnDtls/OrgnlMndt/OrgnlMndtId",
                                 "Original mandate ID is required in suspension request"))

    return v


# ── Dispatch table ────────────────────────────────────────────────────────────

_RULES: dict[str, _Rule] = {
    "001": _rules_credit_transfer,
    "013": _rules_credit_transfer,
    "002": _rules_status_report,
    "014": _rules_status_report,
    "007": _rules_reversal,
    "008": _rules_direct_debit,
    "009": _rules_mandate_initiation,
    "010": _rules_mandate_amendment,
    "011": _rules_mandate_cancellation,
    "012": _rules_mandate_acceptance,
    "017": _rules_mandate_copy,
    "018": _rules_mandate_suspension,
}


# ── Public entry point ────────────────────────────────────────────────────────

def validate(root: etree._Element, message_set: str) -> list[RuleViolation]:
    """Run all business rules for the given message set.

    Args:
        root:        Parsed lxml root element (the Document element).
        message_set: Three-digit message set string e.g. '001'.

    Returns:
        List of RuleViolation — empty means all rules passed.
    """
    rule_fn = _RULES.get(message_set)
    if rule_fn is None:
        return []

    ns_match = re.match(r"\{([^}]+)\}", root.tag)
    ns = ns_match.group(1) if ns_match else ""

    try:
        return rule_fn(root, ns)
    except Exception:
        return []
