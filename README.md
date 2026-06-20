# ISO 20022 Payments Test Automation Platform

Tools for validating, generating, and streaming **ISO 20022 pain** (Payments Initiation) XML messages. Covers message sets 001–018.

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/architecture.html) | System design, data flows, component breakdown, design decisions |
| [Executive Report](docs/executive-report.html) | Business value, effort reduction, and ROI narrative |
| [Executive Summary](docs/executive-summary.md) | Comprehensive project summary — pipeline results, security posture, and artefact inventory |
| [OWASP LLM Security Assessment](docs/owasp-llm-security-report.html) | Full security assessment against OWASP LLM Top 10 (2025) |

## Repository Contents

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
    ├── 001/   hand-crafted and generated XML test messages
    └── 002/   generated XML test messages

docs/
    architecture.html               System architecture document
    executive-report.html           Business value and executive summary
    owasp-llm-security-report.html  OWASP LLM Top 10 security assessment

keys/
    sender_private.pem  Ed25519 private key — signs every Kafka message (gitignored)
    sender_public.pem   Ed25519 public key  — used by receiver to verify signatures

reports/        generated HTML reports (gitignored)
state.db        SQLite pipeline state — sent, processed, duplicates, tampered (gitignored)
docker-compose.yml  Confluent Kafka 7.6 + Kafka UI (ports 9092 and 8080)
```

## Prerequisites

**Python** — [uv](https://docs.astral.sh/uv/) for all script execution and dependency management:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

All scripts declare their own dependencies via PEP 723 inline metadata — no `pip install` or virtual environment setup needed.

**Docker** — required for the Kafka pipeline only:

```bash
# macOS
brew install --cask docker   # or install Docker Desktop from docker.com
```

---

## 1 — Validator

Validates XML messages against an XSD schema and produces a Thoughtworks-branded HTML report.

```bash
# Validate all test files for a message set
uv run ISO20022_validator.py pain.001

# More specific — resolves to schema/pain/001/pain.001.001.13.xsd
uv run ISO20022_validator.py pain.001.001
```

Report written to `reports/<domain>_<timestamp>_report.html`.

### Raw xmllint

```bash
xmllint --schema schema/pain/001/pain.001.001.13.xsd --noout <your-message.xml>
```

---

## 2 — Test Data Generator

Generates synthetic ISO 20022 XML test messages for any schema using Claude (Anthropic API).

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

# Generate ~50 test messages for pain.001
uv run generate_test_data.py pain 001

# Generate for a specific list of message sets
uv run generate_test_data.py pain 001 002 005

# Dot-notation alternative (single set)
uv run generate_test_data.py pain.001

# Generate for all pain message sets
uv run generate_test_data.py pain

# Override the model (default: claude-sonnet-4-6)
uv run generate_test_data.py --model=claude-opus-4-8 pain 001 002
```

Generated files are saved to `test_data/<domain>/<msg_set>/`:

| Prefix | Meaning |
|---|---|
| `gen-pass-NNN.xml` | Valid — passes XSD validation |
| `gen-fail-NNN.xml` | Invalid — deliberate schema violation |
| `gen-edge-NNN.xml` | Valid — tests boundary conditions |

Each run also produces an HTML summary report in `reports/gen_<timestamp>_report.html`.

### Test distribution

| Category | Target | Description |
|---|---|---|
| Pass (70%) | 35 | Valid messages covering diverse field combinations |
| Fail (20%) | 10 | Invalid messages, each with a different violation type |
| Edge (10%) | 5 | Valid messages testing boundary conditions |

Each generated XML is validated by lxml. If a "pass" case fails validation it is automatically re-bucketed as a "fail" file, so every saved file is correctly labelled.

---

## 3 — Kafka Pipeline (Multi-Agent)

An event-driven pipeline that streams test messages through Kafka, validates them, and proves exactly-once processing via a reconciliation report. Every message is **Ed25519-signed** by the sender and verified by the receiver before XSD validation.

```
generate_keys.py  →  keys/sender_private.pem
                          keys/sender_public.pem

sender_agent.py   →   Kafka topics (signed)   →   receiver_agent.py   →   reconciliation_report.py
                                                         ↓ (tampered)
                                                   iso20022.tampered
                            ↓ (fail XSD)
                       iso20022.dlq
```

### Message integrity — Ed25519 digital signatures

Each message sent by `sender_agent.py` carries a base64-encoded Ed25519 signature over the raw XML bytes. The receiver verifies the signature as its **first** check:

- **Signature missing or invalid** → message is forwarded to `iso20022.tampered` and recorded in `tampered_messages`. XSD validation is skipped.
- **Signature valid** → proceeds to XSD validation as normal.

```bash
# Generate key pair once before first run
uv run generate_keys.py
# keys/sender_private.pem  (gitignored — never commit)
# keys/sender_public.pem   (safe to distribute to receivers)
```

### Kafka topics

| Topic | Purpose |
|---|---|
| `iso20022.pain.001` … `iso20022.pain.018` | One topic per message set |
| `iso20022.dlq` | Dead-letter queue — messages that failed XSD validation |
| `iso20022.tampered` | Messages whose Ed25519 signature failed verification |

### Start Kafka

```bash
docker-compose up -d        # starts Confluent Kafka 7.6 on port 9092 + Kafka UI on port 8080
docker-compose logs -f      # follow logs
docker-compose down         # stop and remove
docker-compose stop         # stop without removing
```

### Kafka UI

A visual dashboard for monitoring topics, messages, consumer group lag, and the DLQ in real time.

```
http://localhost:8080
```

Opens automatically once Kafka's healthcheck passes (~30s after `docker-compose up -d`).

| Feature | Where in Kafka UI |
|---|---|
| Browse messages on any topic | Topics → select topic → Messages tab |
| Inspect DLQ payloads | Topics → `iso20022.dlq` → Messages |
| Consumer group lag (receiver progress) | Consumer Groups → `iso20022-receivers` |
| Message throughput graphs | Dashboard |

### Run the pipeline

```bash
# Step 0 (once) — generate Ed25519 key pair
uv run generate_keys.py

# Step 1 — send all test messages to Kafka (signed + idempotent)
uv run sender_agent.py

# Step 2 — start receivers (verify signatures, then validate XSD)
uv run receiver_agent.py
# Press Ctrl+C when all messages are consumed

# Step 3 — generate reconciliation report
uv run reconciliation_report.py
```

### Receiver output

Each receiver thread prefixes every log line with its domain/message-set:

```
[pain.001] listening on iso20022.pain.001
[pain.002] listening on iso20022.pain.002
...
[pain.001] PASS       gen-pass-001.xml
[pain.001] FAIL       gen-fail-003.xml  — Line 12: element 'Ccy' missing
[pain.001] DUPLICATE  gen-pass-001.xml
[pain.001] TAMPERED   gen-pass-007.xml  — signature verification failed
```

### Exactly-once semantics

| Layer | Mechanism |
|---|---|
| Kafka | `acks=all` + `retries=5` → at-least-once delivery |
| Message ID | `sha256(domain + msg_set + xml_bytes)` — content-addressed, stable across retries |
| SQLite | `INSERT OR IGNORE` on `message_id PRIMARY KEY` — second insert is a no-op |
| Offset commit | After DB write — crash between validate and commit causes redeliver, caught as duplicate |

### Reconciliation report

`reports/reconciliation_<timestamp>.html` shows:

- **Pass** — signature verified + XSD valid
- **Fail → DLQ** — signature valid but failed XSD validation, forwarded to `iso20022.dlq`
- **Tampered** — Ed25519 signature missing or invalid, forwarded to `iso20022.tampered`
- **Not Processed** — sent but not yet consumed (receiver still catching up)
- **Duplicates Caught** — re-sent messages detected and skipped

A green "Fully reconciled" verdict appears when every sent message has been processed exactly once.

The report also includes **DuckDB analytics sections** (powered by DuckDB querying `state.db` directly):

| Analytics Section | What it shows |
|---|---|
| Schema Breakdown | Per-message-set pass rate with visual bar chart |
| Validation Error Patterns | Most common XSD error categories ranked by frequency |
| Test Category vs Actual Outcome | How gen-pass / gen-fail / gen-edge files actually performed |
| Tampered Messages by Schema | Count of tampered messages per message set (shown when non-zero) |

### Inspect the state database

```bash
sqlite3 state.db

-- Summary
SELECT validation_status, COUNT(*) FROM processed_messages GROUP BY validation_status;

-- Failed messages
SELECT file_name, message_set, error_detail FROM processed_messages WHERE validation_status = 'fail';

-- Unprocessed
SELECT file_name FROM sent_messages
WHERE message_id NOT IN (SELECT message_id FROM processed_messages);

-- Duplicates
SELECT file_name, detected_at FROM duplicate_events;

-- Tampered messages
SELECT file_name, message_set, detected_at FROM tampered_messages;
```

### Inspect the dead-letter queue

```bash
docker exec -it payments-kafka-1 kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic iso20022.dlq \
  --from-beginning
```

---

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

**Message integrity** — Ed25519 digital signatures (PyCA `cryptography` library):
- Private key (`keys/sender_private.pem`) signs every XML payload before it enters Kafka
- Public key (`keys/sender_public.pem`) is loaded by the receiver at startup
- Messages with a missing or invalid signature are quarantined to `iso20022.tampered` — they never reach the XSD validator
- `keys/` is gitignored so the private key is never committed

**OWASP LLM Top 10 mitigations:**
- **LLM06** — domain argument validated against `^[a-z]{4}(\.\d{3}){0,3}$` before any file I/O
- **LLM10** — lxml parser hardened (`resolve_entities=False`, `no_network=True`, `huge_tree=False`); file count and size caps enforced

---

Thoughtworks Financial Services Practice
