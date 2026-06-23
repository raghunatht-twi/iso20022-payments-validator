# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "anthropic>=0.40,<1.0",
#   "httpx>=0.25,<1.0",
#   "lxml>=5.0,<6.0",
# ]
# ///
"""ISO 20022 test data generation agent."""
from __future__ import annotations

import html
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
from lxml import etree


_BASE_DIR = Path(__file__).parent
SCHEMA_DIR = _BASE_DIR / "schema"
TEST_DATA_DIR = _BASE_DIR / "test_data"
REPORT_DIR = _BASE_DIR / "reports"

_MODEL = "claude-sonnet-4-6"
_MAX_XSD_BYTES = 100_000
_MAX_TOKENS = 32_000
_TARGET_PASS = 35
_TARGET_FAIL = 10
_TARGET_EDGE = 5
_MAX_RETRIES = 3
_RETRY_DELAY_SECS = 10

# LLM10: hardened parser — no external entities, no network, no oversized docs
_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)

# LLM06: reject path-traversal and injection in domain argument
_DOMAIN_RE = re.compile(r"^[a-z]{4}(\.\d{3}){0,3}$")
_XS_NS = "http://www.w3.org/2001/XMLSchema"

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

_CATEGORY_INSTRUCTIONS: dict[str, str] = {
    "pass": (
        "Generate exactly {count} ISO 20022 XML messages that MUST PASS XSD schema validation.\n"
        "Rules — read carefully before generating:\n"
        "1. Use the EXACT namespace URI shown in 'Schema constraints' above — copy it verbatim\n"
        "2. Follow xs:sequence element ORDER precisely — ISO 20022 validation fails on any ordering error\n"
        "3. Include ALL mandatory elements (those without minOccurs='0' in the schema)\n"
        "4. Apply the value formats listed in 'Schema constraints': dates, IBANs, BICs, UUIDs, amounts\n"
        "5. Vary data across messages: different amounts, currencies, parties, dates, identifiers\n"
        "6. Use realistic but fully synthetic / anonymised data (no real PII or account numbers)\n"
        "Self-check each message before including it: trace every element against the XSD sequence."
    ),
    "fail": (
        "Generate exactly {count} ISO 20022 XML messages that DELIBERATELY FAIL schema validation.\n"
        "- Each XML must be well-formed (parseable) but INVALID against the XSD\n"
        "- Use a DIFFERENT violation type for each message:\n"
        "  missing required element, wrong element order, invalid enumeration value,\n"
        "  string exceeding maxLength, malformed IBAN, malformed BIC/SWIFT code,\n"
        "  amount with too many decimal places, missing currency (Ccy) attribute,\n"
        "  negative amount (below minInclusive), invalid or out-of-range date value\n"
        "- State the intended violation type in the description field"
    ),
    "edge": (
        "Generate exactly {count} ISO 20022 XML messages that are VALID but test edge cases.\n"
        "Every message MUST validate — apply the same namespace/ordering/format rules as for pass cases.\n"
        "Cover a different boundary condition in each message:\n"
        "  string fields at their exact maximum allowed length (count characters carefully),\n"
        "  amounts with the maximum permitted decimal places,\n"
        "  all optional fields and attributes present (maximum-population message),\n"
        "  Unicode / international characters in name and address fields (xs:string allows them),\n"
        "  maximum allowed repetitions of any repeating element"
    ),
}

_PROMPT = """\
You are an expert in ISO 20022 financial messaging standards.

XSD Schema ({schema_name}){truncation_note}:
```xml
{xsd_content}
```

Schema constraints (extracted from the XSD above — treat these as ground truth):
{schema_facts}

Task:
{instructions}

Before adding each XML to your response, verify it against these four checks:
  1. xmlns value exactly matches the namespace URI in 'Schema constraints'
  2. Every element is in the correct xs:sequence order (ISO 20022 is strict — order matters)
  3. All required elements are present
  4. All values (IBAN, BIC, date, UUID, amount/Ccy) match the formats in 'Schema constraints'

Respond with a JSON array inside a single code block — no text before or after it.
Each element must have exactly two string fields:
  "xml"         — complete XML document (include declaration and namespace declaration)
  "description" — one sentence describing what this test case covers

```json
[{{"xml": "<?xml version=\\"1.0\\" encoding=\\"UTF-8\\"?>...", "description": "..."}}]
```"""


@dataclass
class GeneratedCase:
    xml: str
    description: str
    requested_category: str
    actual_category: str
    schema_name: str
    file_path: Path | None = None


@dataclass
class SchemaResult:
    schema_path: Path
    cases: list[GeneratedCase] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.cases if c.actual_category == "pass")

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.cases if c.actual_category == "fail")

    @property
    def edge_count(self) -> int:
        return sum(1 for c in self.cases if c.actual_category == "edge")

    @property
    def mismatch_count(self) -> int:
        return sum(1 for c in self.cases if c.requested_category != c.actual_category)


def _validate_domain_arg(domain: str) -> None:
    if not _DOMAIN_RE.match(domain):
        raise ValueError(
            f"'{domain}' is not a valid ISO 20022 domain name. "
            "Expected format: 'pain' or 'pain.001'."
        )


def _find_xsd_files(domain_name: str, msg_set: str | None) -> list[Path]:
    domain_root = SCHEMA_DIR / domain_name
    if not domain_root.is_dir():
        raise FileNotFoundError(
            f"No schema directory for domain '{domain_name}' found in '{SCHEMA_DIR}'."
        )
    if msg_set is not None:
        search_dir = domain_root / msg_set
        if not search_dir.is_dir():
            raise FileNotFoundError(
                f"No schema directory for message set '{msg_set}' under '{domain_root}'."
            )
        return sorted(search_dir.glob("*.xsd"))
    return sorted(domain_root.rglob("*.xsd"))


def _output_dir_for_schema(schema_path: Path) -> Path:
    return TEST_DATA_DIR / schema_path.relative_to(SCHEMA_DIR).parent


def _read_xsd(schema_path: Path) -> tuple[str, bool]:
    raw = schema_path.read_bytes()
    if len(raw) <= _MAX_XSD_BYTES:
        return raw.decode("utf-8"), False
    return raw[:_MAX_XSD_BYTES].decode("utf-8", errors="replace"), True


def _extract_schema_facts(xsd_doc: etree._ElementTree) -> str:
    root = xsd_doc.getroot()
    lines: list[str] = []

    target_ns = root.get("targetNamespace", "")
    if target_ns:
        lines.append(f"Namespace URI (copy verbatim into xmlns): {target_ns}")

    root_elems = root.findall(f"{{{_XS_NS}}}element")
    if root_elems and target_ns:
        name = root_elems[0].get("name", "")
        if name:
            lines.append(f'Root element opening tag: <{name} xmlns="{target_ns}">')

    lines += [
        "Element ordering: xs:sequence is used throughout — any out-of-order element fails validation",
        "Dates (xs:date): YYYY-MM-DD, e.g. 2025-03-15",
        "DateTimes (xs:dateTime): YYYY-MM-DDTHH:MM:SS, e.g. 2025-03-15T10:30:00",
        "IBAN: 2 uppercase letters + 2 digits + up to 30 alphanumeric, e.g. GB29NWBK60161331926819",
        "BIC/SWIFT: exactly 8 or 11 uppercase alphanumeric chars, e.g. NWBKGB2L or NWBKGB2LXXX",
        "Amounts: Ccy attribute is mandatory on every amount element, e.g. <InstdAmt Ccy=\"EUR\">1250.00</InstdAmt>",
        "UETR (UUID v4): xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx, third group starts with 4, fourth starts with 8/9/a/b",
        "ChargeBearerType allowed codes: DEBT, CRED, SHAR, SLEV",
        "PaymentMethod allowed codes: TRF, CHK, TRA",
    ]

    return "\n".join(f"• {line}" for line in lines)


def _build_prompt(
    schema_name: str,
    xsd_content: str,
    truncated: bool,
    schema_facts: str,
    category: str,
    count: int,
) -> str:
    note = " [TRUNCATED — infer full structure from the partial schema shown]" if truncated else ""
    return _PROMPT.format(
        schema_name=schema_name,
        truncation_note=note,
        xsd_content=xsd_content,
        schema_facts=schema_facts,
        instructions=_CATEGORY_INSTRUCTIONS[category].format(count=count),
    )


def _supports_adaptive_thinking(model: str) -> bool:
    return "opus" in model or "fable" in model


def _call_claude(client: anthropic.Anthropic, prompt: str, model: str = _MODEL) -> str:
    extra = {"thinking": {"type": "adaptive"}} if _supports_adaptive_thinking(model) else {}
    with client.messages.stream(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        **extra,
    ) as stream:
        message = stream.get_final_message()
    text_blocks = [b for b in message.content if b.type == "text"]
    if not text_blocks:
        types = [b.type for b in message.content]
        raise ValueError(f"No text block in response (got: {types})")
    return text_blocks[-1].text


def _extract_json(text: str) -> list[dict]:
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No JSON array found in Claude response")


def _actual_category(requested: str, validated: bool) -> str:
    if validated:
        return "edge" if requested == "edge" else "pass"
    return "fail"


def _item_to_case(
    item: dict,
    category: str,
    schema_name: str,
    schema: etree.XMLSchema,
) -> GeneratedCase | None:
    xml_str = item.get("xml", "")
    if not xml_str:
        return None
    try:
        element = etree.fromstring(xml_str.encode(), parser=_SAFE_PARSER)
        validated = schema.validate(element)
    except etree.XMLSyntaxError:
        validated = False
    return GeneratedCase(
        xml=xml_str,
        description=item.get("description", ""),
        requested_category=category,
        actual_category=_actual_category(category, validated),
        schema_name=schema_name,
    )


def _generate_cases_for_category(
    client: anthropic.Anthropic,
    schema_path: Path,
    xsd_content: str,
    truncated: bool,
    schema_facts: str,
    schema: etree.XMLSchema,
    category: str,
    target: int,
    model: str = _MODEL,
) -> list[GeneratedCase]:
    cases: list[GeneratedCase] = []

    for attempt in range(1, _MAX_RETRIES + 1):
        remaining = target - len(cases)
        if remaining <= 0:
            break

        prompt = _build_prompt(schema_path.name, xsd_content, truncated, schema_facts, category, remaining)

        try:
            response_text = _call_claude(client, prompt, model)
            items = _extract_json(response_text)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"      attempt {attempt}: parse error — {exc}", file=sys.stderr)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_SECS)
            continue
        except anthropic.RateLimitError:
            print(f"      attempt {attempt}: rate limited — sleeping 60s", file=sys.stderr)
            time.sleep(60)
            continue
        except anthropic.APIError as exc:
            print(f"      attempt {attempt}: API error — {exc}", file=sys.stderr)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_SECS)
            continue
        except httpx.ReadError as exc:
            print(f"      attempt {attempt}: connection reset — {exc}", file=sys.stderr)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_SECS)
            continue

        for item in items:
            case = _item_to_case(item, category, schema_path.name, schema)
            if case is not None:
                cases.append(case)

        if len(cases) < target and attempt < _MAX_RETRIES:
            time.sleep(2)

    return cases


def _generate_for_schema(client: anthropic.Anthropic, schema_path: Path, model: str = _MODEL) -> SchemaResult:
    result = SchemaResult(schema_path=schema_path)
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] {schema_path.name}")

    try:
        xsd_doc = etree.parse(str(schema_path), _SAFE_PARSER)
        schema = etree.XMLSchema(xsd_doc)
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError) as exc:
        print(f"  Error loading schema: {exc}", file=sys.stderr)
        return result

    xsd_content, truncated = _read_xsd(schema_path)
    schema_facts = _extract_schema_facts(xsd_doc)

    for category, target in (("pass", _TARGET_PASS), ("fail", _TARGET_FAIL), ("edge", _TARGET_EDGE)):
        print(f"    {category:<4}  requesting {target}...", end=" ", flush=True)
        cases = _generate_cases_for_category(
            client, schema_path, xsd_content, truncated, schema_facts, schema, category, target, model
        )
        mismatches = sum(1 for c in cases if c.requested_category != c.actual_category)
        suffix = f"  ({mismatches} recategorised)" if mismatches else ""
        print(f"got {len(cases)}{suffix}")
        result.cases.extend(cases)

    return result


def _save_cases(cases: list[GeneratedCase], output_dir: Path) -> None:
    counters: dict[str, int] = {"pass": 1, "fail": 1, "edge": 1}
    date_stamp = datetime.now().strftime("%m%d%Y")
    for case in cases:
        cat = case.actual_category
        filename = f"gen-{cat}-{counters[cat]:03d}-{date_stamp}.xml"
        case.file_path = output_dir / filename
        case.file_path.write_text(case.xml, encoding="utf-8")
        counters[cat] += 1


def _badge(category: str) -> str:
    colours = {"pass": "#689E78", "fail": "#F2617A", "edge": "#634F7D"}
    colour = colours.get(category, "#47A1AD")
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 10px;'
        f"border-radius:3px;font-size:0.78rem;font-weight:600;"
        f'letter-spacing:0.05em;">{category.upper()}</span>'
    )


def _summary_card(label: str, value: str | int, colour: str) -> str:
    return (
        f'<div style="background:{colour};color:#fff;border-radius:6px;'
        f'padding:1.2rem 2rem;text-align:center;min-width:130px;">'
        f'<div style="font-size:2.2rem;font-weight:700;line-height:1;">{value}</div>'
        f'<div style="font-size:0.82rem;margin-top:6px;opacity:0.88;">{label}</div>'
        f"</div>"
    )


def _schema_rows(result: SchemaResult) -> str:
    rows = []
    for case in result.cases:
        file_name = html.escape(case.file_path.name if case.file_path else "—")
        mismatch = ""
        if case.requested_category != case.actual_category:
            mismatch = (
                f' <span style="color:#CC850A;font-size:0.78em;">'
                f"(requested: {case.requested_category})</span>"
            )
        rows.append(
            "<tr>"
            f"<td style='font-family:monospace;font-size:0.82rem;'>{file_name}</td>"
            f"<td style='text-align:center;'>{_badge(case.actual_category)}{mismatch}</td>"
            f"<td style='font-size:0.87rem;'>{html.escape(case.description)}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _schema_section(result: SchemaResult) -> str:
    output_dir = _output_dir_for_schema(result.schema_path)
    rel = html.escape(str(output_dir.relative_to(_BASE_DIR)))
    name = html.escape(result.schema_path.name)
    mismatch_row = (
        f"<dt>Recategorised</dt><dd>{result.mismatch_count}</dd>"
        if result.mismatch_count
        else ""
    )
    return f"""
    <section>
      <h2>{name}</h2>
      <dl>
        <dt>Output directory</dt><dd>{rel}/</dd>
        <dt>Pass cases</dt><dd>{result.pass_count}</dd>
        <dt>Fail cases</dt><dd>{result.fail_count}</dd>
        <dt>Edge cases</dt><dd>{result.edge_count}</dd>
        <dt>Total</dt><dd>{len(result.cases)}</dd>
        {mismatch_row}
      </dl>
      <table style="margin-top:1rem;">
        <thead>
          <tr>
            <th style="width:22%">File</th>
            <th style="width:10%;text-align:center;">Category</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          {_schema_rows(result)}
        </tbody>
      </table>
    </section>"""


def _generate_report(results: list[SchemaResult], model: str = _MODEL) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    report_path = REPORT_DIR / f"gen_{ts.strftime('%Y%m%d_%H%M%S')}_report.html"

    total = sum(len(r.cases) for r in results)
    total_pass = sum(r.pass_count for r in results)
    total_fail = sum(r.fail_count for r in results)
    total_edge = sum(r.edge_count for r in results)

    css_vars = "\n".join(f"  --{k}: {v};" for k, v in _TW_COLOURS.items())
    cards = (
        _summary_card("Total Generated", total, "#003D4F")
        + _summary_card("Pass Cases", total_pass, "#689E78")
        + _summary_card("Fail Cases", total_fail, "#F2617A")
        + _summary_card("Edge Cases", total_edge, "#634F7D")
        + _summary_card("Schemas", len(results), "#47A1AD")
    )
    schema_sections = "\n".join(_schema_section(r) for r in results)

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>ISO 20022 Test Data Generation Report</title>
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
    .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.5rem; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.5rem 1.5rem; font-size: 0.9rem; }}
    dt {{ font-weight: 600; color: var(--teal-dk); }}
    dd {{ color: #333; word-break: break-all; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--white); font-size: 0.875rem;
             border-radius: 6px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    thead {{ background: var(--teal-dk); color: var(--white); }}
    thead th {{ padding: 0.75rem 1rem; text-align: left; font-weight: 600;
                font-size: 0.78rem; letter-spacing: 0.06em; text-transform: uppercase; }}
    tbody td {{ padding: 0.75rem 1rem; vertical-align: top; border-bottom: 1px solid #d8e0e4; line-height: 1.55; }}
    tbody tr:nth-child(even) {{ background: var(--mist); }}
    tbody tr:hover {{ background: #d5dfe4; }}
    footer {{ text-align: center; font-size: 0.78rem; color: #888;
              padding: 2rem; border-top: 1px solid #ccc; margin-top: 1rem; }}
  </style>
</head>
<body>
  <header>
    <h1>ISO 20022 Test Data Generation Report</h1>
    <p>Generated {ts.strftime("%d %B %Y at %H:%M:%S")} &mdash; model: {model}</p>
  </header>
  <article>
    <section>
      <h2>Generation Summary</h2>
      <div class="cards">{cards}</div>
    </section>
    {schema_sections}
  </article>
  <footer>generate_test_data &mdash; Thoughtworks Financial Services Practice</footer>
</body>
</html>"""

    report_path.write_text(doc, encoding="utf-8")
    return report_path


def main() -> None:
    args = sys.argv[1:]
    model_override: str | None = None

    if args and args[0].startswith("--model="):
        model_override = args[0].split("=", 1)[1]
        args = args[1:]

    if len(args) < 1:
        print("Usage: uv run generate_test_data.py [--model=<id>] <domain> [<message-set> ...]", file=sys.stderr)
        print("  uv run generate_test_data.py pain                  # all pain message sets", file=sys.stderr)
        print("  uv run generate_test_data.py pain 001              # pain.001 only", file=sys.stderr)
        print("  uv run generate_test_data.py pain 001 002 005      # pain.001, pain.002, pain.005", file=sys.stderr)
        print("  uv run generate_test_data.py pain.001              # pain.001 only", file=sys.stderr)
        print("  uv run generate_test_data.py --model=claude-sonnet-4-6 pain 001", file=sys.stderr)
        sys.exit(1)

    domain_arg = args[0].strip().lower()
    msg_set_args = [a.strip() for a in args[1:]]

    try:
        _validate_domain_arg(domain_arg)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    parts = domain_arg.split(".")
    domain_name = parts[0]
    dotted_msg_set = parts[1] if len(parts) >= 2 else None
    msg_sets = msg_set_args if msg_set_args else ([dotted_msg_set] if dotted_msg_set else [])

    try:
        if msg_sets:
            xsd_files: list[Path] = []
            seen: set[Path] = set()
            for ms in msg_sets:
                for p in _find_xsd_files(domain_name, ms):
                    if p not in seen:
                        seen.add(p)
                        xsd_files.append(p)
        else:
            xsd_files = _find_xsd_files(domain_name, None)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not xsd_files:
        scope = f"'{domain_name}'" + (f" / {msg_sets}" if msg_sets else "")
        print(f"Error: No XSD files found for {scope}", file=sys.stderr)
        sys.exit(1)

    model = model_override or _MODEL
    scope_label = domain_name + (
        " / message sets: " + ", ".join(msg_sets) if msg_sets else " (all message sets)"
    )
    print("ISO 20022 Test Data Generator")
    print(f"Domain  : {scope_label}")
    print(f"Schemas : {len(xsd_files)}")
    print(f"Targets : {_TARGET_PASS} pass + {_TARGET_FAIL} fail + {_TARGET_EDGE} edge per schema")
    print(f"Model   : {model}")

    client = anthropic.Anthropic()
    results: list[SchemaResult] = []

    for schema_path in xsd_files:
        output_dir = _output_dir_for_schema(schema_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        result = _generate_for_schema(client, schema_path, model)
        _save_cases(result.cases, output_dir)
        results.append(result)
        rel = output_dir.relative_to(_BASE_DIR)
        print(
            f"    saved  {result.pass_count}P / {result.fail_count}F / "
            f"{result.edge_count}E  →  {rel}/"
        )

    report_path = _generate_report(results, model)
    grand_total = sum(len(r.cases) for r in results)
    print(f"\nTotal   : {grand_total} test files across {len(results)} schema(s)")
    print(f"Report  : {report_path}")


if __name__ == "__main__":
    main()
