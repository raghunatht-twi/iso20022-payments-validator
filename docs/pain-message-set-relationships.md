# How pain.001 Relates to Other pain Message Set Schemas

The 12 pain message sets form three distinct workflow families, and pain.001 sits at the centre of the largest one. Here is how they all connect.

---

## The Three Workflow Families

### Family 1 — Credit Transfer (pain.001 is the entry point)

```
Initiating Party                    Debtor's Bank                    Creditor's Bank
      │                                   │                                 │
      │──── pain.001 ──────────────────>  │                                 │
      │     (Credit Transfer Initiation)  │                                 │
      │                                   │──── interbank messages ──────>  │
      │  <──── pain.002 ─────────────────  │                                 │
      │     (Payment Status Report)       │                                 │
      │                                   │                                 │
      │──── pain.007 ──────────────────>  │  (if reversal needed)           │
           (Payment Reversal)
```

| Message | Role | References pain.001 via |
|---|---|---|
| **pain.001** | Initiates the credit transfer | — (originator) |
| **pain.002** | Bank reports acceptance, rejection, or pending status back to the initiator | `OrgnlMsgId` + `OrgnlMsgNmId` pointing to the pain.001 `MsgId` |
| **pain.007** | Requests reversal of a previously sent payment | `OrgnlMsgId`, `OrgnlPmtInfId`, `OrgnlEndToEndId` — all copied from pain.001 fields |

**pain.002** can report at two levels:
- **Group level** (`OrgnlGrpInfAndSts`) — the whole pain.001 batch accepted/rejected
- **Transaction level** (`TxInfAndSts`) — individual transactions within the batch, each with its own status and reason code

**pain.007** mirrors the pain.001 structure (`OrgnlPmtInfAndRvsl` parallels `PmtInf`, `TxInf` parallels `CdtTrfTxInf`) and carries a reversal reason code (e.g. `DUPL` = duplicate, `FRAD` = fraud, `TECH` = technical issue).

---

### Family 2 — Creditor-Initiated Payment (pain.013/014 mirror pain.001/002)

```
Creditor                            Creditor's Bank → Debtor's Bank → Debtor
      │
      │──── pain.013 ──────────────────>  bank
      │     (Creditor Payment Activation Request)
      │
      │  <──── pain.014 ─────────────────  bank
           (Creditor Payment Activation Request Status Report)
```

| Message | Equivalent | Difference |
|---|---|---|
| **pain.013** | pain.001 | Initiated by the **creditor** (payee), not the debtor. Same XML structure — `CdtrPmtActvtnReq` uses identical `PmtInf` / `CdtTrfTxInf` / IBAN / BIC fields. Used in request-to-pay scenarios. |
| **pain.014** | pain.002 | Status report on the pain.013, same pattern of `OrgnlGrpInfAndSts` + `OrgnlMsgId` back-reference. |

---

### Family 3 — Direct Debit & Mandate Lifecycle (pain.008–012, 017, 018)

Direct debit is the mirror image of credit transfer — the creditor pulls money from the debtor rather than the debtor pushing it. The mandate (pre-authorisation from the debtor) must exist first.

```
Mandate Lifecycle:
pain.009 ──> pain.012          Set up mandate + bank acceptance
    │
pain.010 ──> pain.012          Amend mandate + bank acceptance
    │
pain.011                       Cancel mandate
pain.017                       Request a copy of the mandate
pain.018                       Suspend the mandate temporarily

Direct Debit Execution (once mandate exists):
pain.008 ──> pain.002          Initiate collection + status report
```

| Message | Purpose | Key back-reference |
|---|---|---|
| **pain.008** | Direct Debit Initiation — creditor instructs their bank to pull funds from the debtor | `MndtRltdInf/MndtId` links each transaction to the mandate set up by pain.009 |
| **pain.009** | Mandate Initiation Request — set up a new direct debit mandate | — (originator) |
| **pain.010** | Mandate Amendment Request — change terms of an existing mandate | `OrgnlMndtId` back to pain.009 |
| **pain.011** | Mandate Cancellation Request — cancel a mandate | `OrgnlMndtId` back to pain.009 |
| **pain.012** | Mandate Acceptance Report — bank confirms or rejects the mandate | `OrgnlMndtId`; `Accptd` = true/false |
| **pain.017** | Mandate Copy Request — retrieve a copy of an existing mandate | `OrgnlMndtId` |
| **pain.018** | Mandate Suspension Request — temporarily suspend a mandate | `OrgnlMndtId`; suspension reason code |

**pain.008 vs pain.001** — structurally similar (both have `GrpHdr`, `PmtInf`, transaction blocks, IBAN/BIC fields, amounts) but directionally opposite: pain.001 debits the initiator's own account; pain.008 debits the *debtor's* account on behalf of the creditor. The payment method code changes from `TRF` to `DD`.

---

## Complete Relationship Map

```
                    ┌─────────────────────────────────────────────┐
                    │          CREDIT TRANSFER FAMILY             │
                    │                                             │
          initiate  │  pain.001 ──────────────> pain.002          │
                    │     │         status                        │
                    │     └──────────────────> pain.007           │
                    │                reversal                     │
                    └─────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────────┐
                    │      CREDITOR-INITIATED PAYMENT FAMILY      │
                    │                                             │
          initiate  │  pain.013 ──────────────> pain.014          │
                    │              status                         │
                    └─────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────────┐
                    │       DIRECT DEBIT & MANDATE FAMILY         │
                    │                                             │
                    │  pain.009 ──> pain.012   (mandate setup)    │
                    │  pain.010 ──> pain.012   (amend)            │
                    │  pain.011              (cancel)             │
                    │  pain.017              (copy request)       │
                    │  pain.018              (suspend)            │
                    │                                             │
                    │  pain.008 ──> pain.002   (collect funds)    │
                    └─────────────────────────────────────────────┘
```

---

## The Key Design Principle Across All Families

Every response or follow-up message carries **back-references** to the original — always `OrgnlMsgId` (copying the `MsgId` from the initiating message) plus `OrgnlEndToEndId` at transaction level. This chain of references is what enables end-to-end traceability across the full payment lifecycle, from initiation through status reporting, reversal, or mandate changes.
