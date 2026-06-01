# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lxml>=5.0",
# ]
# ///
"""ISO 20022 XML message validator with Thoughtworks-branded HTML report output."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from lxml import etree


_BASE_DIR = Path(__file__).parent
SCHEMA_DIR = _BASE_DIR / "schema"
TEST_DATA_DIR = _BASE_DIR / "test_data"
REPORT_DIR = _BASE_DIR / "reports"

_NS_RE = re.compile(r"\{[^}]+\}")

_DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "pain": "Payments Initiation",
    "pacs": "Payments Clearing and Settlement",
    "camt": "Cash Management",
    "acmt": "Account Management",
    "auth": "Authorities Financial Investigations",
    "colr": "Collateral Management",
    "reda": "Reference Data",
    "remt": "Payments Remittance Advice",
}

# Each rule: (compiled pattern applied to the raw lxml message, suggestion template).
# Template placeholders {0}, {1} map to regex capture groups.
# Use {{}} for literal braces inside suggestion text (processed via str.format).
_SUGGESTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"not expected.*Expected is[^{]*\{[^}]+\}(\w+)", re.DOTALL),
        "Element is in the wrong position or misspelled. "
        "The schema expects '{0}' at this point — verify the element sequence in the XSD.",
    ),
    (
        re.compile(r"\[facet 'enumeration'\].*value '([^']+)' is not an element of the set"),
        "'{0}' is not an allowed code for this field. Consult the schema enumeration. "
        "Common sets — ChargeBearerType: DEBT, CRED, SHAR, SLEV; "
        "PaymentMethod: TRF, CHK, TRA; "
        "ChequeType: CCHQ, CCCH, BCHQ, DRFT, ELDR; "
        "ExchangeRateType: SPOT, SALE, AGRD; "
        "Priority: NORM, HIGH.",
    ),
    # Ccy-specific rule must precede the general pattern rule to avoid being shadowed.
    (
        re.compile(r"attribute 'Ccy'"),
        "The 'Ccy' attribute is missing or invalid. "
        "Currency codes must be exactly 3 uppercase letters (e.g. EUR, USD, GBP).",
    ),
    (
        re.compile(r"\[facet 'pattern'\].*not accepted by the pattern '([^']+)'"),
        "Value does not match the required format pattern. "
        "Common issues — IBAN: 2 uppercase letters + 2 digits + 1-30 alphanumeric; "
        "BIC: 8 or 11 uppercase alphanumeric characters; "
        "UETR (UUID v4): third group must start with '4', fourth with '8', '9', 'a', or 'b'. "
        "Schema pattern: {0}",
    ),
    (
        re.compile(r"Missing child element.*Expected is[^{]*\{[^}]+\}(\w+)"),
        "Required child element '{0}' is absent. "
        "Insert it in the correct sequence position within the parent element.",
    ),
    (
        re.compile(r"\[facet 'minLength'\].*length of 0"),
        "Field is empty but requires at least one character. Provide a non-empty value.",
    ),
    (
        re.compile(r"\[facet 'maxLength'\].*length of (\d+).*maximum.*?(\d+)"),
        "Value is {0} characters — maximum allowed is {1}. Shorten the value.",
    ),
    (
        re.compile(r"\[facet 'minInclusive'\].*value '([^']+)'.*minimum value.*'([^']+)'"),
        "'{0}' is below the minimum allowed ({1}). Amounts and rates must be zero or positive.",
    ),
    (
        re.compile(r"\[facet 'totalDigits'\]"),
        "Too many digits in total. ISO 20022 amounts allow at most 18 digits in total.",
    ),
    (
        re.compile(r"\[facet 'fractionDigits'\]"),
        "Too many decimal places. "
        "Amounts allow up to 5 decimal places; exchange rates allow up to 10.",
    ),
]

_FALLBACK_SUGGESTION = (
    "Review this element against the XSD definition. "
    "Check value format, length constraints, allowed codes, and element ordering."
)

_TW: dict[str, str] = {
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


@dataclass(frozen=True)
class ValidationError:
    message: str
    suggestion: str


@dataclass
class ValidationResult:
    file_name: str
    file_path: Path
    passed: bool
    errors: list[ValidationError] = field(default_factory=list)


def _strip_ns(text: str) -> str:
    return _NS_RE.sub("", text)


def _suggest_fix(raw_message: str) -> str:
    for pattern, template in _SUGGESTION_RULES:
        m = pattern.search(raw_message)
        if m:
            return template.format(*m.groups())
    return _FALLBACK_SUGGESTION


def _domain_label(domain: str) -> str:
    base = domain.split(".")[0].lower()
    return _DOMAIN_DESCRIPTIONS.get(base, "ISO 20022 Message Domain")


def find_schema(domain: str) -> Path:
    matches = list(SCHEMA_DIR.glob(f"{domain}*.xsd"))
    if not matches:
        raise FileNotFoundError(
            f"No XSD file matching '{domain}*.xsd' found in '{SCHEMA_DIR}'."
        )
    if len(matches) > 1:
        names = ", ".join(p.name for p in sorted(matches))
        raise ValueError(
            f"Multiple XSD files match '{domain}': {names}. "
            "Provide a more specific domain name (e.g. 'pain.001.001.13')."
        )
    return matches[0]


def load_schema(schema_path: Path) -> etree.XMLSchema:
    doc = etree.parse(str(schema_path))
    return etree.XMLSchema(doc)


def find_test_files(domain: str) -> list[Path]:
    domain_dir = TEST_DATA_DIR / domain
    if not domain_dir.is_dir():
        raise FileNotFoundError(
            f"Test data directory '{domain_dir}' does not exist."
        )
    files = sorted(domain_dir.glob("*.xml"))
    if not files:
        raise FileNotFoundError(f"No XML test files found in '{domain_dir}'.")
    return files


def validate_file(xml_path: Path, schema: etree.XMLSchema) -> ValidationResult:
    try:
        doc = etree.parse(str(xml_path))
    except etree.XMLSyntaxError as exc:
        return ValidationResult(
            file_name=xml_path.name,
            file_path=xml_path,
            passed=False,
            errors=[ValidationError(
                message=f"XML syntax error: {exc}",
                suggestion=(
                    "Fix the XML syntax error — check for unclosed tags, "
                    "invalid characters, or a missing namespace declaration."
                ),
            )],
        )

    schema.validate(doc)
    errors = [
        ValidationError(
            message=f"Line {e.line}: {_strip_ns(str(e.message))}",
            suggestion=_suggest_fix(str(e.message)),
        )
        for e in schema.error_log
    ]
    return ValidationResult(
        file_name=xml_path.name,
        file_path=xml_path,
        passed=not errors,
        errors=errors,
    )


def _badge(passed: bool) -> str:
    colour = "var(--green)" if passed else "var(--coral)"
    label = "PASS" if passed else "FAIL"
    return (
        f'<span style="background:{colour};color:#fff;padding:3px 12px;'
        f'border-radius:3px;font-size:0.8rem;font-weight:600;letter-spacing:0.05em;">'
        f"{label}</span>"
    )


def _errors_html(errors: list[ValidationError]) -> str:
    if not errors:
        return '<td style="color:#888;font-style:italic;">—</td>'
    items = "".join(f"<li>{e.message}</li>" for e in errors)
    return f'<td><ol style="margin:0;padding-left:1.1rem;">{items}</ol></td>'


def _suggestions_html(errors: list[ValidationError]) -> str:
    if not errors:
        return '<td style="color:#888;font-style:italic;">—</td>'
    items = "".join(f"<li>{e.suggestion}</li>" for e in errors)
    return f'<td><ol style="margin:0;padding-left:1.1rem;">{items}</ol></td>'


def _summary_card(label: str, value: str | int, css_var: str) -> str:
    return (
        f'<div style="background:var({css_var});color:#fff;border-radius:6px;'
        f'padding:1.2rem 2rem;text-align:center;min-width:130px;">'
        f'<div style="font-size:2.2rem;font-weight:700;line-height:1;">{value}</div>'
        f'<div style="font-size:0.82rem;margin-top:6px;opacity:0.88;">{label}</div>'
        f"</div>"
    )


def generate_report(
    domain: str,
    schema_path: Path,
    results: list[ValidationResult],
) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    report_path = REPORT_DIR / f"{domain}_{ts.strftime('%Y%m%d_%H%M%S')}_report.html"

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    pass_rate = f"{passed / total * 100:.0f}%" if total else "N/A"

    css_vars = "\n".join(f"  --{k}: {v};" for k, v in _TW.items())

    rows = "\n".join(
        f"<tr>"
        f"<td style='font-family:monospace;font-size:0.85rem;'>{r.file_name}</td>"
        f"<td style='text-align:center;'>{_badge(r.passed)}</td>"
        f"{_errors_html(r.errors)}"
        f"{_suggestions_html(r.errors)}"
        f"</tr>"
        for r in results
    )

    cards = (
        _summary_card("Total Files", total, "--teal-dk")
        + _summary_card("Passed", passed, "--green")
        + _summary_card("Failed", failed, "--coral")
        + _summary_card("Pass Rate", pass_rate, "--teal")
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>ISO 20022 Validation — {domain}</title>
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
    h2 {{
      font-family: 'Bitter', serif;
      font-size: 1.2rem;
      font-weight: 600;
      color: var(--teal-dk);
      border-bottom: 2px solid var(--teal-dk);
      padding-bottom: 0.4rem;
      margin-bottom: 1.2rem;
    }}
    .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.5rem 1.5rem; font-size: 0.9rem; }}
    dt {{ font-weight: 600; color: var(--teal-dk); }}
    dd {{ color: #333; word-break: break-all; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--white); font-size: 0.875rem; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    thead {{ background: var(--teal-dk); color: var(--white); }}
    thead th {{ padding: 0.75rem 1rem; text-align: left; font-weight: 600; font-size: 0.78rem; letter-spacing: 0.06em; text-transform: uppercase; }}
    tbody td {{ padding: 0.75rem 1rem; vertical-align: top; border-bottom: 1px solid #d8e0e4; line-height: 1.55; }}
    tbody tr:nth-child(even) {{ background: var(--mist); }}
    tbody tr:hover {{ background: #d5dfe4; }}
    ol {{ list-style-position: outside; padding-left: 1.1rem; }}
    ol li {{ margin-bottom: 0.4rem; }}
    footer {{ text-align: center; font-size: 0.78rem; color: #888; padding: 2rem; border-top: 1px solid #ccc; margin-top: 1rem; }}
  </style>
</head>
<body>
  <header>
    <h1>ISO 20022 Validation Report</h1>
    <p>Generated {ts.strftime("%d %B %Y at %H:%M:%S")}</p>
  </header>

  <article>
    <section>
      <h2>Executive Summary</h2>
      <div class="cards">
        {cards}
      </div>
    </section>

    <section>
      <h2>Test Scope</h2>
      <dl>
        <dt>Domain</dt>
        <dd>{domain.upper()} — {_domain_label(domain)}</dd>
        <dt>Schema File</dt>
        <dd>{schema_path.name}</dd>
        <dt>Schema Path</dt>
        <dd>{schema_path.resolve()}</dd>
        <dt>Test Data Directory</dt>
        <dd>{(TEST_DATA_DIR / domain).resolve()}</dd>
        <dt>Files Tested</dt>
        <dd>{total}</dd>
        <dt>Run Timestamp</dt>
        <dd>{ts.strftime("%Y-%m-%d %H:%M:%S")}</dd>
      </dl>
    </section>

    <section>
      <h2>Validation Results</h2>
      <table>
        <thead>
          <tr>
            <th style="width:22%">File</th>
            <th style="width:8%;text-align:center">Status</th>
            <th style="width:35%">Failure Reason(s)</th>
            <th style="width:35%">Suggested Fix(es)</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </section>
  </article>

  <footer>ISO20022_validator &mdash; Thoughtworks Financial Services Practice</footer>
</body>
</html>"""

    report_path.write_text(html, encoding="utf-8")
    return report_path


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: uv run ISO20022_validator.py <domain>", file=sys.stderr)
        print("Example: uv run ISO20022_validator.py pain", file=sys.stderr)
        sys.exit(1)

    domain = sys.argv[1].strip().lower()

    try:
        schema_path = find_schema(domain)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Schema   : {schema_path}")

    try:
        schema = load_schema(schema_path)
    except etree.XMLSchemaParseError as exc:
        print(f"Failed to load schema: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        test_files = find_test_files(domain)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Files    : {len(test_files)} in test_data/{domain}/\n")

    results = [validate_file(f, schema) for f in test_files]

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.file_name}")

    report_path = generate_report(domain, schema_path, results)

    passed_count = sum(1 for r in results if r.passed)
    print(f"\nResults  : {passed_count}/{len(results)} passed")
    print(f"Report   : {report_path}")


if __name__ == "__main__":
    main()
