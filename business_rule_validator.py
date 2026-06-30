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

_CTRL_SUM_TOLERANCE: float = 0.01

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


def _xp(root: etree._Element, path: str, ns: str) -> list[etree._Element]:
    return root.xpath(path, namespaces={"p": ns})


def _txt(root: etree._Element, path: str, ns: str) -> str | None:
    els = _xp(root, path, ns)
    if els and els[0].text:
        return els[0].text.strip()
    return None


def _is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


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


def _violation(
    rule_id: str,
    severity: str,
    field: str,
    msg: str,
    actual: str | None = None,
    expected: str | None = None,
) -> RuleViolation:
    return RuleViolation(
        rule_id=rule_id, severity=severity, field_path=field,
        message=msg, actual=actual, expected=expected,
    )


def _check_ibans(
    root: etree._Element, ns: str, xpath: str, rule_id: str, field_label: str,
) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, xpath, ns):
        val = el.text.strip() if el.text else ""
        if val and not _validate_iban(val):
            violations.append(_violation(
                rule_id, "ERROR", field_label,
                "IBAN failed ISO 7064 MOD-97 check digit validation",
                actual=val,
            ))
    return violations


def _check_bics(
    root: etree._Element, ns: str, xpath: str, rule_id: str, field_label: str,
) -> list[RuleViolation]:
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


def _check_group_nb_of_txs(root: etree._Element, ns: str) -> list[RuleViolation]:
    nb_txs_str = _txt(root, "//p:GrpHdr/p:NbOfTxs", ns)
    actual_count = len(_xp(root, "//p:CdtTrfTxInf", ns))
    if nb_txs_str is None:
        return []
    try:
        declared = int(nb_txs_str)
    except ValueError:
        return [_violation("BR-001", "ERROR", "GrpHdr/NbOfTxs",
                           "NbOfTxs is not a valid integer", actual=nb_txs_str)]
    if declared != actual_count:
        return [_violation(
            "BR-001", "ERROR", "GrpHdr/NbOfTxs",
            f"Declared NbOfTxs ({declared}) does not match actual transaction count ({actual_count})",
            actual=str(actual_count), expected=str(declared),
        )]
    return []


def _check_group_ctrl_sum(root: etree._Element, ns: str) -> list[RuleViolation]:
    ctrl_sum_str = _txt(root, "//p:GrpHdr/p:CtrlSum", ns)
    if ctrl_sum_str is None:
        return []
    try:
        declared_sum = float(ctrl_sum_str)
    except ValueError:
        return [_violation("BR-002", "ERROR", "GrpHdr/CtrlSum",
                           "CtrlSum is not a valid decimal", actual=ctrl_sum_str)]
    amounts = [
        float(el.text.strip())
        for el in _xp(root, "//p:CdtTrfTxInf/p:Amt/p:InstdAmt", ns)
        if el.text and _is_float(el.text.strip())
    ]
    if not amounts:
        return []
    actual_sum = round(sum(amounts), 5)
    if abs(declared_sum - actual_sum) > _CTRL_SUM_TOLERANCE:
        return [_violation(
            "BR-002", "ERROR", "GrpHdr/CtrlSum",
            f"CtrlSum ({declared_sum}) does not match sum of InstdAmt ({actual_sum})",
            actual=str(actual_sum), expected=str(declared_sum),
        )]
    return []


def _check_pmt_inf_counts(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for i, pmt in enumerate(_xp(root, "//p:PmtInf", ns), start=1):
        pmt_txs = _xp(pmt, "p:CdtTrfTxInf", ns)

        pmt_nb = _txt(pmt, "p:NbOfTxs", ns)
        if pmt_nb is not None:
            try:
                if int(pmt_nb) != len(pmt_txs):
                    violations.append(_violation(
                        "BR-004", "ERROR", f"PmtInf[{i}]/NbOfTxs",
                        f"PmtInf NbOfTxs ({pmt_nb}) != transaction count ({len(pmt_txs)})",
                        actual=str(len(pmt_txs)), expected=pmt_nb,
                    ))
            except ValueError:
                pass

        pmt_ctrl = _txt(pmt, "p:CtrlSum", ns)
        if pmt_ctrl is not None:
            try:
                pmt_amts = [
                    float(el.text.strip())
                    for el in _xp(pmt, "p:CdtTrfTxInf/p:Amt/p:InstdAmt", ns)
                    if el.text and _is_float(el.text.strip())
                ]
                if pmt_amts:
                    actual_sum = round(sum(pmt_amts), 5)
                    if abs(float(pmt_ctrl) - actual_sum) > _CTRL_SUM_TOLERANCE:
                        violations.append(_violation(
                            "BR-003", "ERROR", f"PmtInf[{i}]/CtrlSum",
                            f"PmtInf CtrlSum ({pmt_ctrl}) != sum of InstdAmt ({actual_sum})",
                            actual=str(actual_sum), expected=pmt_ctrl,
                        ))
            except ValueError:
                pass
    return violations


def _check_end_to_end_uniqueness(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    seen: set[str] = set()
    for el in _xp(root, "//p:CdtTrfTxInf/p:PmtId/p:EndToEndId", ns):
        val = el.text.strip() if el.text else ""
        if val in seen:
            violations.append(_violation(
                "BR-005", "ERROR", "CdtTrfTxInf/PmtId/EndToEndId",
                "Duplicate EndToEndId within the message", actual=val,
            ))
        else:
            seen.add(val)
    return violations


def _check_uetrs(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, "//p:CdtTrfTxInf/p:PmtId/p:UETR", ns):
        val = el.text.strip() if el.text else ""
        if val and not _validate_uuid4(val):
            violations.append(_violation(
                "BR-006", "ERROR", "CdtTrfTxInf/PmtId/UETR",
                "UETR must be a valid UUID v4 (third group starts with '4', "
                "fourth group starts with 8/9/a/b)",
                actual=val,
            ))
    return violations


def _check_amounts_positive(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, "//p:CdtTrfTxInf/p:Amt/p:InstdAmt", ns):
        raw = el.text.strip() if el.text else "0"
        try:
            if float(raw) <= 0:
                violations.append(_violation(
                    "BR-007", "ERROR", "CdtTrfTxInf/Amt/InstdAmt",
                    "InstdAmt must be greater than zero", actual=raw,
                ))
        except ValueError:
            pass
    return violations


def _check_execution_dates(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, "//p:ReqdExctnDt/p:Dt", ns):
        try:
            if el.text and date.fromisoformat(el.text.strip()) < _today():
                violations.append(_violation(
                    "BR-011", "WARNING", "PmtInf/ReqdExctnDt/Dt",
                    "Requested execution date is in the past",
                    actual=el.text.strip(), expected=f">= {_today().isoformat()}",
                ))
        except ValueError:
            pass
    return violations


def _check_msg_id(root: etree._Element, ns: str) -> list[RuleViolation]:
    if not _txt(root, "//p:GrpHdr/p:MsgId", ns):
        return [_violation("BR-012", "ERROR", "GrpHdr/MsgId", "MsgId is missing or empty")]
    return []


def _rules_credit_transfer(root: etree._Element, ns: str) -> list[RuleViolation]:
    return (
        _check_group_nb_of_txs(root, ns)
        + _check_group_ctrl_sum(root, ns)
        + _check_pmt_inf_counts(root, ns)
        + _check_end_to_end_uniqueness(root, ns)
        + _check_uetrs(root, ns)
        + _check_amounts_positive(root, ns)
        + _check_ibans(root, ns, "//p:DbtrAcct/p:Id/p:IBAN",         "BR-008", "DbtrAcct/Id/IBAN")
        + _check_ibans(root, ns, "//p:CdtrAcct/p:Id/p:IBAN",         "BR-008", "CdtrAcct/Id/IBAN")
        + _check_bics( root, ns, "//p:DbtrAgt/p:FinInstnId/p:BICFI",  "BR-009", "DbtrAgt/FinInstnId/BICFI")
        + _check_bics( root, ns, "//p:CdtrAgt/p:FinInstnId/p:BICFI",  "BR-009", "CdtrAgt/FinInstnId/BICFI")
        + _check_bics( root, ns, "//p:IntrmyAgt1/p:FinInstnId/p:BICFI","BR-009", "IntrmyAgt1/FinInstnId/BICFI")
        + _check_currencies(root, ns)
        + _check_execution_dates(root, ns)
        + _check_msg_id(root, ns)
    )


def _rules_status_report(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for el in _xp(root, "//p:OrgnlGrpInfAndSts/p:GrpSts", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _GRP_STATUS_CODES:
            violations.append(_violation(
                "BR-020", "ERROR", "OrgnlGrpInfAndSts/GrpSts",
                f"GrpSts '{val}' is not a recognised ISO 20022 group status code",
                actual=val, expected=f"one of {sorted(_GRP_STATUS_CODES)}",
            ))

    for el in _xp(root, "//p:TxInfAndSts/p:TxSts", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _TX_STATUS_CODES:
            violations.append(_violation(
                "BR-021", "ERROR", "TxInfAndSts/TxSts",
                f"TxSts '{val}' is not a recognised ISO 20022 transaction status code",
                actual=val,
            ))

    for el in _xp(root, "//p:StsRsnInf/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _RJCT_REASON_CODES:
            violations.append(_violation(
                "BR-022", "WARNING", "StsRsnInf/Rsn/Cd",
                f"Status reason code '{val}' is not in the common ISO 20022 reason code set",
                actual=val,
            ))

    for block in _xp(root, "//p:OrgnlGrpInfAndSts", ns):
        if not _txt(block, "p:OrgnlMsgId", ns):
            violations.append(_violation("BR-023", "ERROR", "OrgnlGrpInfAndSts/OrgnlMsgId",
                                         "Original message reference OrgnlMsgId is missing or empty"))
    return violations


def _rules_reversal(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for i, tx in enumerate(_xp(root, "//p:TxInf", ns), start=1):
        if not _txt(tx, "p:OrgnlEndToEndId", ns):
            violations.append(_violation(
                "BR-030", "ERROR", f"OrgnlPmtInfAndRvsl/TxInf[{i}]/OrgnlEndToEndId",
                "OrgnlEndToEndId is required for each reversal transaction — "
                "it must match the EndToEndId from the original pain.001",
            ))

    for el in _xp(root, "//p:RvslRsnInf/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _RVSL_REASON_CODES:
            violations.append(_violation(
                "BR-031", "WARNING", "RvslRsnInf/Rsn/Cd",
                f"Reversal reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_RVSL_REASON_CODES)}",
            ))

    for el in _xp(root, "//p:OrgnlInstdAmt", ns):
        raw = el.text.strip() if el.text else "0"
        try:
            if float(raw) <= 0:
                violations.append(_violation(
                    "BR-032", "ERROR", "TxInf/OrgnlInstdAmt",
                    "OrgnlInstdAmt must be greater than zero", actual=raw,
                ))
        except ValueError:
            pass

    violations += _check_currencies(root, ns)
    return violations


def _rules_direct_debit(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    nb_txs_str = _txt(root, "//p:GrpHdr/p:NbOfTxs", ns)
    actual_count = len(_xp(root, "//p:DrctDbtTxInf", ns))
    if nb_txs_str is not None:
        try:
            declared = int(nb_txs_str)
            if declared != actual_count:
                violations.append(_violation(
                    "BR-040", "ERROR", "GrpHdr/NbOfTxs",
                    f"Declared NbOfTxs ({declared}) != actual DrctDbtTxInf count ({actual_count})",
                    actual=str(actual_count), expected=str(declared),
                ))
        except ValueError:
            pass

    ctrl_sum_str = _txt(root, "//p:GrpHdr/p:CtrlSum", ns)
    if ctrl_sum_str is not None:
        try:
            declared_sum = float(ctrl_sum_str)
            amounts = [
                float(el.text.strip())
                for el in _xp(root, "//p:DrctDbtTxInf/p:InstdAmt", ns)
                if el.text and _is_float(el.text.strip())
            ]
            if amounts:
                actual_sum = round(sum(amounts), 5)
                if abs(declared_sum - actual_sum) > _CTRL_SUM_TOLERANCE:
                    violations.append(_violation(
                        "BR-041", "ERROR", "GrpHdr/CtrlSum",
                        f"CtrlSum ({declared_sum}) != sum of InstdAmt ({actual_sum})",
                        actual=str(actual_sum), expected=str(declared_sum),
                    ))
        except ValueError:
            pass

    for i, tx in enumerate(_xp(root, "//p:DrctDbtTxInf", ns), start=1):
        if not _txt(tx, "p:MndtRltdInf/p:MndtId", ns):
            violations.append(_violation(
                "BR-042", "ERROR", f"DrctDbtTxInf[{i}]/MndtRltdInf/MndtId",
                "MndtId is required — each direct debit transaction must reference a mandate",
            ))

    violations += _check_ibans(root, ns, "//p:CdtrAcct/p:Id/p:IBAN", "BR-043", "CdtrAcct/Id/IBAN")
    violations += _check_ibans(root, ns, "//p:DbtrAcct/p:Id/p:IBAN", "BR-043", "DbtrAcct/Id/IBAN")
    violations += _check_bics(root, ns, "//p:CdtrAgt/p:FinInstnId/p:BICFI", "BR-044", "CdtrAgt/FinInstnId/BICFI")
    violations += _check_bics(root, ns, "//p:DbtrAgt/p:FinInstnId/p:BICFI", "BR-044", "DbtrAgt/FinInstnId/BICFI")
    violations += _check_currencies(root, ns)

    for el in _xp(root, "//p:ReqdColltnDt", ns):
        try:
            if el.text and date.fromisoformat(el.text.strip()) < _today():
                violations.append(_violation(
                    "BR-046", "WARNING", "PmtInf/ReqdColltnDt",
                    "Requested collection date is in the past",
                    actual=el.text.strip(), expected=f">= {_today().isoformat()}",
                ))
        except ValueError:
            pass

    return violations


def _rules_mandate_initiation(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for el in _xp(root, "//p:MndtReqId", ns):
        if not el.text or not el.text.strip():
            violations.append(_violation("BR-050", "ERROR", "Mndt/MndtReqId",
                                         "MndtReqId is required and must not be empty"))

    for el in _xp(root, "//p:TrckgInd", ns):
        val = el.text.strip().lower() if el.text else ""
        if val not in ("true", "false", "1", "0"):
            violations.append(_violation(
                "BR-051", "ERROR", "Mndt/TrckgInd",
                "TrckgInd must be 'true' or 'false'", actual=el.text,
            ))

    violations += _check_ibans(root, ns, "//p:CdtrAcct/p:Id/p:IBAN", "BR-052", "CdtrAcct/Id/IBAN")
    violations += _check_ibans(root, ns, "//p:DbtrAcct/p:Id/p:IBAN", "BR-052", "DbtrAcct/Id/IBAN")
    violations += _check_bics(root, ns, "//p:CdtrAgt/p:FinInstnId/p:BICFI", "BR-053", "CdtrAgt/FinInstnId/BICFI")
    violations += _check_bics(root, ns, "//p:DbtrAgt/p:FinInstnId/p:BICFI", "BR-053", "DbtrAgt/FinInstnId/BICFI")
    return violations


def _rules_mandate_amendment(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for el in _xp(root, "//p:AmdmntRsn/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _AMD_REASON_CODES:
            violations.append(_violation(
                "BR-060", "WARNING", "AmdmntRsn/Rsn/Cd",
                f"Amendment reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_AMD_REASON_CODES)}",
            ))

    for el in _xp(root, "//p:Mndt/p:MndtId", ns):
        if not el.text or not el.text.strip():
            violations.append(_violation("BR-061", "ERROR", "UndrlygAmdmntDtls/Mndt/MndtId",
                                         "MndtId is required in mandate amendment"))
    return violations


def _rules_mandate_cancellation(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for el in _xp(root, "//p:CxlRsn/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _CXL_REASON_CODES:
            violations.append(_violation(
                "BR-070", "WARNING", "CxlRsn/Rsn/Cd",
                f"Cancellation reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_CXL_REASON_CODES)}",
            ))

    for el in _xp(root, "//p:OrgnlMndt/p:OrgnlMndtId", ns):
        if not el.text or not el.text.strip():
            violations.append(_violation("BR-071", "ERROR", "UndrlygCxlDtls/OrgnlMndt/OrgnlMndtId",
                                         "Original mandate ID is required in cancellation request"))
    return violations


def _rules_mandate_acceptance(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for el in _xp(root, "//p:AccptncRslt/p:Accptd", ns):
        val = el.text.strip().lower() if el.text else ""
        if val not in ("true", "false", "1", "0"):
            violations.append(_violation(
                "BR-080", "ERROR", "UndrlygAccptncDtls/AccptncRslt/Accptd",
                "Accptd must be 'true' or 'false'", actual=el.text,
            ))

    for block in _xp(root, "//p:UndrlygAccptncDtls", ns):
        accptd = _txt(block, "p:AccptncRslt/p:Accptd", ns)
        if accptd and accptd.lower() in ("false", "0"):
            if not _txt(block, "p:AccptncRslt/p:RjctRsn/p:Rsn/p:Cd", ns):
                violations.append(_violation(
                    "BR-081", "WARNING", "AccptncRslt/RjctRsn/Rsn/Cd",
                    "Rejection reason code is recommended when Accptd=false",
                ))
    return violations


def _rules_mandate_copy(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []
    for el in _xp(root, "//p:OrgnlMndt/p:OrgnlMndtId", ns):
        if not el.text or not el.text.strip():
            violations.append(_violation("BR-090", "ERROR", "UndrlygCpyReqDtls/OrgnlMndt/OrgnlMndtId",
                                         "Original mandate ID is required in copy request"))
    return violations


def _rules_mandate_suspension(root: etree._Element, ns: str) -> list[RuleViolation]:
    violations: list[RuleViolation] = []

    for el in _xp(root, "//p:SspnsnRsn/p:Rsn/p:Cd", ns):
        val = el.text.strip() if el.text else ""
        if val and val not in _SSPN_REASON_CODES:
            violations.append(_violation(
                "BR-100", "WARNING", "SspnsnRsn/Rsn/Cd",
                f"Suspension reason code '{val}' is not in the recognised set",
                actual=val, expected=f"one of {sorted(_SSPN_REASON_CODES)}",
            ))

    for el in _xp(root, "//p:OrgnlMndt/p:OrgnlMndtId", ns):
        if not el.text or not el.text.strip():
            violations.append(_violation("BR-101", "ERROR", "UndrlygSspnsnDtls/OrgnlMndt/OrgnlMndtId",
                                         "Original mandate ID is required in suspension request"))
    return violations


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


def validate(root: etree._Element, message_set: str) -> list[RuleViolation]:
    """Run all business rules for the given message set; empty list means all passed."""
    rule_fn = _RULES.get(message_set)
    if rule_fn is None:
        return []
    ns_match = re.match(r"\{([^}]+)\}", root.tag)
    ns = ns_match.group(1) if ns_match else ""
    return rule_fn(root, ns)
