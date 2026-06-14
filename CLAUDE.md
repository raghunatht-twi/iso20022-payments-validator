# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

This repository contains ISO 20022 XSD schemas and an XML validator for the **pain** (Payments Initiation) domain, covering message sets 001–018. The primary schema is **pain.001.001.13** — Customer Credit Transfer Initiation, Version 13. Namespace: `urn:iso:std:iso:20022:tech:xsd:pain.001.001.13`.

## Directory Structure

```
schema/
└── <domain>/               e.g. pain/
    └── <message-set>/      e.g. 001/
        └── <domain>.<message-set>.<variant>.<version>.xsd

test_data/
└── <domain>/               e.g. pain/
    └── <message-set>/      e.g. 001/
        └── *.xml

reports/                    generated HTML reports (gitignored)
```

Schemas and test fixtures are co-organised by the same `domain/message-set` hierarchy as the ISO 20022 naming convention.

## Running the Validator

```bash
# Validate all pain.001 test files and produce an HTML report
uv run ISO20022_validator.py pain.001

# More specific — resolves to schema/pain/001/pain.001.001.13.xsd
uv run ISO20022_validator.py pain.001.001
```

The domain argument accepts any prefix valid against `^[a-z]{4}(\.\d{3}){0,3}$`. Passing only `pain` will error if multiple message-set schemas exist (by design — be specific).

## Raw xmllint

```bash
xmllint --schema schema/pain/001/pain.001.001.13.xsd --noout <your-message.xml>
```

## Message Hierarchy

```
Document
└── CstmrCdtTrfInitn (CustomerCreditTransferInitiationV13)
    ├── GrpHdr (GroupHeader114)           — message-level: MsgId, CreDtTm, NbOfTxs, InitgPty
    ├── PmtInf (PaymentInstruction51) [1..*]  — payment batch: Dbtr, DbtrAcct, DbtrAgt, ReqdExctnDt
    │   └── CdtTrfTxInf (CreditTransferTransaction76) [1..*]  — per-transaction: Cdtr, CdtrAcct, Amt
    └── SplmtryData [0..*]
```

**Two-level grouping:** `PmtInf` groups transactions sharing a debtor account and execution date. `CdtTrfTxInf` carries individual creditor, amount, and remittance info.

## ISO 20022 Abbreviation Conventions

The schema uses systematic abbreviations throughout — these are not arbitrary:

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
| `Ultmt` | Ultimate |
| `Intrmy` | Intermediary |
| `Prtry` | Proprietary |
| `Cd` | Code |

## Key Schema Patterns

- **`XxxYyyChoice` types**: `xs:choice` — exactly one of two alternatives, typically `Cd` (external code) or `Prtry` (proprietary free-text).
- **`ExternalXxx1Code` types**: Unbounded string — values come from external ISO 20022 code lists, not enumerated in the XSD itself.
- **`Max35Text`, `Max140Text`, etc.**: String length constraints.
- **Account identification** (`AccountIdentification4Choice`): Either `IBAN` or `Othr` (generic).
- **Financial institution identification** (`BranchAndFinancialInstitutionIdentification8`): Identified by BIC (`BICFI`) or LEI or name/address.
- **Amount** (`AmountType4Choice`): Either `InstdAmt` (instructed amount with currency) or `EqvtAmt` (equivalent amount with exchange rate).
- **`ChargeBearerType1Code`**: `DEBT` (debtor pays), `CRED` (creditor pays), `SHAR` (shared), `SLEV` (service level).
- **`PaymentMethod3Code`**: `TRF` (credit transfer), `CHK` (cheque), `TRA` (transfer with advice).

## Project-Wide Conventions

### Thoughtworks Brand Guidelines (v3 / canonical)

All output deliverables (HTML reports, dashboards, visualisations) must use the Thoughtworks design palette:

```css
--white:   #FFFFFF
--mist:    #EDF1F3   /* page background */
--black:   #000000
--teal-dk: #003D4F   /* primary headings, borders */
--coral:   #F2617A   /* warnings, overrun indicators */
--amber:   #CC850A   /* cautions, deferred revenue */
--green:   #689E78   /* positive metrics, on-track */
--teal:    #47A1AD   /* secondary accents */
--plum:    #634F7D   /* supplementary callouts */
```

Fonts: `Inter` (body) + `Bitter` (headings), both from Google Fonts. Max content width 1000px. Article padding 3.5rem 4rem. Self-contained except for Google Fonts CDN.

### Python

Use `uv` for all Python work — running scripts and installing dependencies. Never execute a Python script directly without `uv`.

```bash
uv run script.py
uv add <package>
```

