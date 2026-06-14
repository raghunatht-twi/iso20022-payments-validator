# ISO 20022 Payments Validator

Tools for validating and generating synthetic test data for **ISO 20022 pain** (Payments Initiation) XML messages. Covers message sets 001–018.

## Contents

```
schema/
└── pain/
    ├── 001/   pain.001.001.13.xsd  — Customer Credit Transfer Initiation
    ├── 002/   pain.002.001.15.xsd  — Payment Status Report
    ├── 007/   pain.007.001.13.xsd  — Customer Payment Reversal
    ├── 008/   pain.008.001.12.xsd  — Customer Direct Debit Initiation
    ├── 009/   pain.009.001.08.xsd  — Mandate Initiation Request
    ├── 010/   pain.010.001.08.xsd  — Mandate Amendment Request
    ├── 011/   pain.011.001.08.xsd  — Mandate Cancellation Request
    ├── 012/   pain.012.001.08.xsd  — Mandate Acceptance Report
    ├── 013/   pain.013.001.12.xsd  — Creditor Payment Activation Request
    ├── 014/   pain.014.001.12.xsd  — Creditor Payment Activation Request Status Report
    ├── 017/   pain.017.001.04.xsd  — Mandate Copy Request
    └── 018/   pain.018.001.04.xsd  — Mandate Suspension Request

test_data/
└── pain/
    └── 001/   hand-crafted and generated XML test messages

reports/        generated HTML reports (gitignored)
```

## Prerequisites

[uv](https://docs.astral.sh/uv/) — used for all Python execution and dependency management.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Both scripts declare their own dependencies via PEP 723 inline metadata, so no `pip install` or virtual-env setup is needed.

## Validator

Validates XML messages against an XSD schema and produces a Thoughtworks-branded HTML report.

```bash
# Validate all test files for a message set
uv run ISO20022_validator.py pain.001

# More specific — resolves to schema/pain/001/pain.001.001.13.xsd
uv run ISO20022_validator.py pain.001.001
```

The report is written to `reports/<domain>_<timestamp>_report.html`.

### Raw xmllint

```bash
xmllint --schema schema/pain/001/pain.001.001.13.xsd --noout <your-message.xml>
```

## Test Data Generator

Generates synthetic ISO 20022 XML test messages for any schema in the repo using Claude (Anthropic API).

```bash
# Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Generate ~50 test messages for pain.001 (35 pass, 10 fail, 5 edge cases)
uv run generate_test_data.py pain 001

# Dot-notation alternative
uv run generate_test_data.py pain.001

# Generate for all pain message sets
uv run generate_test_data.py pain

# Override the model (default: claude-sonnet-4-6)
uv run generate_test_data.py --model=claude-opus-4-8 pain 001
```

Generated files are saved to `test_data/<domain>/<msg_set>/`:

| Prefix | Meaning |
|---|---|
| `gen-pass-NNN.xml` | Intended to pass XSD validation |
| `gen-fail-NNN.xml` | Intended to fail XSD validation (deliberate violations) |
| `gen-edge-NNN.xml` | Valid but tests boundary conditions |

Each run also produces an HTML summary report in `reports/gen_<timestamp>_report.html` listing every generated file, its category, and a one-line description.

### Test distribution

| Category | Target | Description |
|---|---|---|
| Pass (70%) | 35 | Valid messages covering diverse field combinations |
| Fail (20%) | 10 | Invalid messages, each with a different violation type |
| Edge (10%) | 5 | Valid messages testing boundary conditions |

Each generated XML is validated against the XSD by lxml. If a "pass" case turns out to be invalid, it is automatically re-bucketed as a "fail" file (and vice versa), so every file is correctly labelled regardless of what Claude produced.

## pain.001 Message Structure

```
Document
└── CstmrCdtTrfInitn  (CustomerCreditTransferInitiationV13)
    ├── GrpHdr         — MsgId, CreDtTm, NbOfTxs, CtrlSum, InitgPty
    ├── PmtInf [1..*]  — Dbtr, DbtrAcct, DbtrAgt, ReqdExctnDt, PmtMtd, ChrgBr
    │   └── CdtTrfTxInf [1..*]  — Cdtr, CdtrAcct, CdtrAgt, Amt, RmtInf
    └── SplmtryData [0..*]
```

## Common ISO 20022 Abbreviations

| Abbreviation | Meaning |
|---|---|
| `Cdtr` / `Dbtr` | Creditor / Debtor |
| `Agt` | Agent (bank) |
| `Acct` | Account |
| `Amt` | Amount |
| `PmtInf` | Payment Information |
| `CdtTrf` | Credit Transfer |
| `GrpHdr` | Group Header |
| `MsgId` | Message Identification |
| `NbOfTxs` | Number of Transactions |
| `CtrlSum` | Control Sum |
| `ReqdExctnDt` | Requested Execution Date |
| `ChrgBr` | Charge Bearer |
| `RmtInf` | Remittance Information |

## Security

Both scripts apply OWASP LLM Top 10 mitigations:

- **LLM06** — domain argument validated against `^[a-z]{4}(\.\d{3}){0,3}$` before any file I/O
- **LLM10** — lxml parser configured with `resolve_entities=False`, `no_network=True`, `huge_tree=False`; file count and size caps enforced

---

Thoughtworks Financial Services Practice
