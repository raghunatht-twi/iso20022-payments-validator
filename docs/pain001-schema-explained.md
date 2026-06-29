# pain.001 — Customer Credit Transfer Initiation

**Full name:** `CustomerCreditTransferInitiationV13`
**Namespace:** `urn:iso:std:iso:20022:tech:xsd:pain.001.001.13`
**Purpose:** A corporate or individual instructs their bank to send one or more payments to one or more recipients. This is the message that kicks off a credit transfer — it flows from the initiating party (corporate/ERP system) to their bank.

---

## The Three-Level Hierarchy

```
Document
└── CstmrCdtTrfInitn  (CustomerCreditTransferInitiation)
    ├── GrpHdr         [1..1]  Group Header      — one per message
    ├── PmtInf         [1..*]  Payment Info      — one per debtor account / execution date
    │   └── CdtTrfTxInf [1..*]  Transaction Info  — one per individual payment
    └── SplmtryData    [0..*]  Supplementary
```

Every `xs:sequence` in ISO 20022 is **strict** — elements must appear in exactly this order or validation fails.

---

## Level 1 — Group Header (`GrpHdr`)

The envelope for the whole message. There is exactly one.

| Field | Abbrev | Type | Notes |
|---|---|---|---|
| Message ID | `MsgId` | Max35Text | Unique reference assigned by the initiator |
| Creation Date/Time | `CreDtTm` | ISODateTime | When the message was created |
| Number of Transactions | `NbOfTxs` | integer | Total count of `CdtTrfTxInf` blocks across all `PmtInf` — must match exactly |
| Control Sum | `CtrlSum` | decimal | Sum of all instructed amounts — business-rule check, not enforced by XSD |
| Initiating Party | `InitgPty` | PartyIdentification | The company or person sending the instruction (name, org ID, LEI) |
| Authorisation | `Authstn` | optional | Pre-authorisation codes if required by the bank |
| Forwarding Agent | `FwdgAgt` | optional | Intermediary that forwarded the message |

---

## Level 2 — Payment Information (`PmtInf`)

Groups all transactions that share the same **debtor** (sender), **account**, **execution date**, and **payment type**. A message can have many `PmtInf` blocks — e.g. one per currency, or one per cost centre.

### Debtor side (who is paying)

| Field | Abbrev | What it identifies |
|---|---|---|
| `Dbtr` | Debtor | Name, postal address, LEI/org ID of the payer |
| `DbtrAcct` | Debtor Account | IBAN or proprietary account number to debit |
| `DbtrAgt` | Debtor Agent | The payer's bank, identified by BIC (`BICFI`) or clearing system member ID |
| `DbtrAgtAcct` | optional | Specific account at the debtor's bank |

### Execution and payment type

| Field | Abbrev | Notes |
|---|---|---|
| `PmtMtd` | Payment Method | `TRF` (transfer), `CHK` (cheque), `TRA` (transfer + advice) |
| `ReqdExctnDt` | Requested Execution Date | Date (or datetime) the bank should execute — `Dt` or `DtTm` choice |
| `PmtTpInf` | Payment Type Info | Service level (`SEPA`, `URGP`, `NURG`), instruction priority (`HIGH`/`NORM`), local instrument, category purpose |
| `ChrgBr` | Charge Bearer | Who bears the fees: `DEBT` (payer), `CRED` (recipient), `SHAR` (each their own), `SLEV` (follow service-level rules) |
| `BtchBookg` | Batch Booking | Boolean — if true, the bank posts one combined debit; if false, one debit per transaction |

---

## Level 3 — Credit Transfer Transaction (`CdtTrfTxInf`)

One block per individual payment. This is where the money and the recipient live.

### Payment identification

| Field | Abbrev | Notes |
|---|---|---|
| `InstrId` | Instruction ID | Reference assigned by the initiator (internal use) |
| `EndToEndId` | End-to-End ID | **Mandatory.** Passed unaltered through every bank in the chain to the recipient — the key traceability reference |
| `UETR` | Unique End-to-End Transaction Ref | UUID v4 — mandated by SWIFT gpi; enables real-time payment tracking across correspondents |
| `TxId` | Transaction ID | Optional reference assigned by the first agent |

### Amount

| Field | Notes |
|---|---|
| `InstdAmt` with `@Ccy` | Instructed amount — a specific sum in a specific currency (e.g. EUR 15,000.00). Up to 18 digits, 5 decimal places. Min value > 0. |
| `EqvtAmt` | Alternative: amount expressed in one currency, to be converted into another (e.g. "send USD 10,000 equivalent in EUR") |

Exactly one of `InstdAmt` or `EqvtAmt` is allowed — this is an `xs:choice`.

### Creditor side (who receives the money)

| Field | Abbrev | What it identifies |
|---|---|---|
| `Cdtr` | Creditor | Name, postal address, LEI/org ID, or private individual ID of the recipient |
| `CdtrAcct` | Creditor Account | IBAN or proprietary account to credit |
| `CdtrAgt` | Creditor Agent | Recipient's bank — BIC, LEI, or name+address |
| `CdtrAgtAcct` | optional | Specific account at the creditor's bank |
| `UltmtCdtr` | Ultimate Creditor | Optional: the actual end-beneficiary if different from the account holder |
| `UltmtDbtr` | Ultimate Debtor | Optional: the actual originator if different from the debtor account |

### Routing and intermediaries

| Field | Notes |
|---|---|
| `IntrmyAgt1/2/3` | Up to 3 correspondent/intermediary banks in the chain, each identified by BIC |
| `InstrForCdtrAgt` | Free-text instructions for the creditor's bank (e.g. "PHONBEN" — phone beneficiary before payment) |
| `InstrForDbtrAgt` | Free-text instructions for the debtor's bank |

### Purpose and remittance

| Field | Abbrev | Notes |
|---|---|---|
| `Purp` | Purpose | ISO external purpose code (`SUPP` = supplier payment, `SALA` = salary, `BEXP` = business expenses, `ENRG` = energy, etc.) or proprietary code |
| `RmtInf` | Remittance Information | Either `Ustrd` (unstructured — free text, max 140 chars) or `Strd` (structured — creditor reference, invoice numbers, tax info) |
| `RgltryRptg` | Regulatory Reporting | Required for certain cross-border payments (e.g. central bank reporting) |
| `Tax` | Tax information | Applicable tax records for the transaction |

### Mandate reference (for credit transfers linked to a mandate)

| Field | Notes |
|---|---|
| `CdtTrfMandateData` | Links the transaction to a pre-existing payment mandate (pain.009) |

---

## Key Schema Patterns

**`XxxChoice` types** — `xs:choice` means exactly one of the alternatives must appear. Examples:
- `AccountIdentification4Choice` → either `IBAN` or `Othr` (generic account)
- `AmountType4Choice` → either `InstdAmt` or `EqvtAmt`
- `DateAndDateTime2Choice` → either `Dt` (date only) or `DtTm` (datetime)

**External code lists** — types like `ExternalCategoryPurpose1Code` and `ExternalServiceLevel1Code` are defined as `xs:string` in the XSD (no enumeration). The valid values come from ISO 20022's published external code lists, not from the schema itself. The XSD will accept any string — validation against the code list is a business-rule check done by the bank.

**Strict sequencing** — every `xs:sequence` mandates exact element order. `CdtTrfTxInf` must have `PmtId` first, then `Amt`, and only then `Cdtr`/`CdtrAcct`. Placing `CdtrAgt` before `Cdtr` is the most common failure in this codebase (56% of errors are wrong-order violations).

**String constraints** — `Max35Text`, `Max140Text`, `Max70Text` are length-bounded strings. No further content validation in the XSD.

---

## A Complete Real-World Example in Plain English

> **Acme Corporation** (debtor, IBAN: `DE89370400440532013000`, bank: Deutsche Bank `DEUTDEDB`) wants to send two payments on **3 June 2026**:
> 1. **€15,000** to **TechSupplier GmbH** (IBAN: `DE87200400600301401300`, bank: Commerzbank `COBADEFFXXX`) — purpose code `SUPP`, remittance "Invoice INV-2026-042"
> 2. **€8,500** to **Jane Doe** (IBAN: `DE91100000000123456789`) — purpose code `BEXP`, remittance "Expense reimbursement June 2026"
>
> Total: €23,500 (`CtrlSum`). Both payments are SEPA credit transfers at normal priority, charges following service-level rules (`SLEV`). The bank is instructed to execute on one specific date, with one `PmtInf` block covering both transactions.
