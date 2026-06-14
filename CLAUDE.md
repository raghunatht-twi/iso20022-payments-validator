# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

This repository contains ISO 20022 XSD schemas and a suite of tools for the **pain** (Payments Initiation) domain, covering message sets 001–018:

- **Validator** — validates XML messages against XSD schemas, produces HTML reports
- **Test Data Generator** — AI agent that generates synthetic XML test fixtures using Claude
- **Kafka Pipeline** — multi-agent system that streams messages through Kafka, validates them, and produces a reconciliation report proving exactly-once processing

## Directory Structure

```
schema/
└── <domain>/               e.g. pain/
    └── <message-set>/      e.g. 001/
        └── <domain>.<message-set>.<variant>.<version>.xsd

test_data/
└── <domain>/               e.g. pain/
    └── <message-set>/      e.g. 001/
        └── *.xml           hand-crafted and generated (gen-pass-NNN, gen-fail-NNN, gen-edge-NNN)

reports/                    generated HTML reports (gitignored)
state.db                    SQLite pipeline state — sent, processed, duplicates (gitignored)
docker-compose.yml          Confluent Kafka 7.6 in KRaft mode (single broker, port 9092)
```

Schemas and test fixtures are co-organised by the same `domain/message-set` hierarchy as the ISO 20022 naming convention.

## Scripts

### ISO20022_validator.py

Validates XML test files against an XSD schema and produces a Thoughtworks-branded HTML report.

```bash
uv run ISO20022_validator.py pain.001       # all pain.001 test files
uv run ISO20022_validator.py pain.001.001   # resolves to pain.001.001.13.xsd
```

Domain argument must match `^[a-z]{4}(\.\d{3}){0,3}$`. Passing only `pain` errors if multiple message-set schemas exist (by design).

### generate_test_data.py

AI agent (calls Claude via Anthropic API) that generates ~50 synthetic XML test messages per XSD, with 70% pass / 20% fail / 10% edge distribution. Each generated XML is validated by lxml and re-bucketed if it doesn't match its intended category.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
uv run generate_test_data.py pain 001                        # one message set
uv run generate_test_data.py pain                            # all message sets
uv run generate_test_data.py --model=claude-opus-4-8 pain 001
```

Default model: `claude-sonnet-4-6`. Adaptive thinking enabled automatically for Opus/Fable models only. Generated files: `gen-pass-NNN.xml`, `gen-fail-NNN.xml`, `gen-edge-NNN.xml`.

### sender_agent.py

Discovers all XML test files under `test_data/` and publishes each to its Kafka topic (`iso20022.<domain>.<msg_set>`). Idempotent — re-runs skip files already recorded in `state.db`. Requires Kafka running.

```bash
uv run sender_agent.py
```

Message ID = `sha256(domain + msg_set + xml_bytes)` — scoped to prevent cross-domain collisions.

### receiver_agent.py

Spawns one consumer thread per domain/message-set (auto-discovered from `schema/`). Each thread:
1. Checks `state.db` for duplicates (`INSERT OR IGNORE` on `message_id PRIMARY KEY`)
2. Validates XML against the XSD using lxml
3. On pass: records to `processed_messages`
4. On fail: forwards to `iso20022.dlq`, records error in `processed_messages`
5. Commits Kafka offset only after DB write (guarantees at-least-once + idempotent = exactly-once)

```bash
uv run receiver_agent.py    # Ctrl+C to stop
```

### reconciliation_report.py

Reads `state.db` and produces a Thoughtworks-branded HTML report proving every sent message was processed exactly once. Shows pass / fail / not-processed / duplicates-caught.

```bash
uv run reconciliation_report.py
```

## Kafka Topics

| Topic | Purpose |
|---|---|
| `iso20022.pain.001` … `iso20022.pain.018` | One topic per message set |
| `iso20022.dlq` | Dead-letter queue — failed validation messages |

Consumer group: `iso20022-receivers`. Broker: `localhost:9092`.

## State Database (state.db)

SQLite, WAL mode, three tables:

| Table | Contents |
|---|---|
| `sent_messages` | Every message published by the sender |
| `processed_messages` | One row per unique message — status: `pass` or `fail` |
| `duplicate_events` | Every re-delivery / re-send caught by the receiver |

Inspect:
```bash
sqlite3 state.db
sqlite3 state.db "SELECT validation_status, COUNT(*) FROM processed_messages GROUP BY validation_status;"
```

## Running the Full Pipeline

```bash
docker-compose up -d                  # start Kafka (wait ~30s)
uv run sender_agent.py                # publish all test messages
uv run receiver_agent.py              # consume, validate, deduplicate (Ctrl+C when done)
uv run reconciliation_report.py       # HTML reconciliation report
docker-compose down                   # stop Kafka
```

## Message Hierarchy (pain.001)

```
Document
└── CstmrCdtTrfInitn (CustomerCreditTransferInitiationV13)
    ├── GrpHdr (GroupHeader114)           — MsgId, CreDtTm, NbOfTxs, InitgPty
    ├── PmtInf (PaymentInstruction51) [1..*]  — Dbtr, DbtrAcct, DbtrAgt, ReqdExctnDt
    │   └── CdtTrfTxInf (CreditTransferTransaction76) [1..*]  — Cdtr, CdtrAcct, Amt
    └── SplmtryData [0..*]
```

## ISO 20022 Abbreviation Conventions

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
- **`ChargeBearerType1Code`**: `DEBT`, `CRED`, `SHAR`, `SLEV`.
- **`PaymentMethod3Code`**: `TRF`, `CHK`, `TRA`.
- **xs:sequence is strict** — element ordering is mandatory throughout; any deviation fails validation.

## Project-Wide Conventions

### Thoughtworks Brand Guidelines (v3 / canonical)

All HTML output (reports, dashboards) must use the Thoughtworks design palette:

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

Use `uv` for all Python work. Never execute a Python script directly without `uv`.

```bash
uv run script.py
uv add <package>
```

All scripts use PEP 723 inline dependency metadata (`# /// script ... # ///`) so `uv run` installs dependencies automatically.

### Security (OWASP LLM Top 10)

- **LLM06** — domain argument validated against `^[a-z]{4}(\.\d{3}){0,3}$` before any file I/O; rejects path traversal and injection
- **LLM10** — lxml parser hardened (`resolve_entities=False`, `no_network=True`, `huge_tree=False`); file count and size caps enforced in all scripts
