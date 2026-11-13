# Special audit invoicing

The expanded application and current requirements are documented in
`APPLICATION_DESIGN.md`. That document supersedes the earlier strict-month and
demo-only South Africa assumptions below: Dubai includes uninvoiced backlog,
South Africa supports assisted processing, and new runs require all three report
jobs and explicit three-source evidence. Country rules are now configurable.

This workflow extends Veritas with deterministic specialist agents for the Dubai
SOP. These agents execute explicit business rules; they do not let a language
model choose financial amounts, portal actions, or email recipients.

Live integration requires an approved gateway implementing the adapter contract
in `veritas/invoicing.py`. No Smart Cert or Flex endpoints, credentials, screen
selectors, or South Africa policy were supplied. Live mode therefore fails closed
until configured. Demo mode uses synthetic contract data and never contacts a
portal or sends email. Demo results are not accounting documents.

## Run locally

Open `http://127.0.0.1:8765/#invoicing` after starting `start-web.ps1` for the
invoicing dashboard. Dubai and South Africa sample runs load automatically in
demo mode and stop at draft review. The screen supports WIP import, workflow
advancement, evidence downloads, recorded approvals and simulated delivery.
Proposal extraction remains available at `#proposals`. Rebuild the UI with
`cd ui; npm.cmd run build` after source changes, and restart the Python server
after backend changes.

Use the existing Python environment:

```powershell
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo create scripts\wip-demo.csv --month 2026-11
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo list
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo advance RUN_ID
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo approve RUN_ID --reviewer "Reviewer name"
```

Inspect the saved run before each approval. Approval resumes execution
automatically to the next gate; repeat at the final invoice gate to deliver.
Approval is a local recorded reviewer
attestation, not enterprise identity authentication. Production deployments need
an authenticated access layer and authorized reviewers.

CSV and XLSX inputs use the SOP column headings: Country, Business Unit, Invoice
Status, Start Date, Audit Type, Flex Contract Number, Amount. Currency, PO Number
and Comments may also be provided. Include currency for financial validation.
The month is `YYYY-MM`. Country codes `AE` and `ZA` and the `Contracting Country`
column heading are recognized. Up to 50 report preamble rows are skipped when
the complete table header is found. Dubai business units are Dubai CER, Dubai CER Food and
Dubai Cyber Security; statuses are Completed, Confirmed and Frozen. Other rows
are excluded. South Africa is available for demo testing only; see
`INVOICING_MEETING_NOTES.md` for its additional fields and unresolved policies.

## Local HTTP API

Start the existing server with `python -m veritas.web`. Invoice endpoints share
its loopback binding and cross-origin write protections. The dashboard uses
explicit demo mode. Use `?mode=demo` for simulation; the API default is live.

- `POST /api/invoicing/import?mode=demo&month=2026-11&country=Dubai&name=wip.csv`
  with raw CSV/XLSX bytes creates a run.
- `GET /api/invoicing?mode=demo` lists runs.
- `GET /api/invoicing/RUN_ID?mode=demo` returns state and evidence.
- `POST /api/invoicing/RUN_ID/advance?mode=demo` with `{}` advances the run.
- `POST /api/invoicing/RUN_ID/approve?mode=demo` with
  `{"reviewer":"Reviewer name"}` records the pending approval.
- `POST /api/invoicing/seed-demo?mode=demo` with `{}` loads the two dashboard
  samples once, preserving their later progress on repeated requests.
- `GET /api/invoicing/RUN_ID/artifact?mode=demo&item=ITEM_ID&stage=download_draft`
  serves a hash-verified local artifact; allowed stages are `contract`,
  `download_draft` and `download_final`.

CLI state is under `outputs/invoicing/MODE`; server state is under
`outputs/web/invoicing/MODE`. State includes financial and customer information;
apply your organization's filesystem permissions and retention policy.

## Production handover

Provide Smart Cert and Flex URLs, supported API documentation or an approved RPA
gateway, authentication method, report formats and the customer email provider.
The gateway must enforce idempotency keys for mutations, validate its own access
rights, return evidence for checks, and distinguish pending operations from
failures. Test against a sandbox using real signed contract fixtures before
enabling production invoice issuance or delivery. Existing Smart Cert-to-Flex
transfer remains owned by the portals; the workflow observes its status.

Amount mismatches, missing metadata, unhandled charges, missing reports and
unconfigured portals must stop progression. Do not resolve an uncertain external
operation by creating another invoice; reconcile the original idempotency key
with the gateway first.

## Gateway wire contract

Set `VERITAS_INVOICE_GATEWAY_URL` and `VERITAS_INVOICE_GATEWAY_TOKEN` in the
server/CLI environment. Credentials are read from the environment, not saved in
workflow state. The adapter POSTs to `URL/execute` over HTTPS with bearer
authentication and an `Idempotency-Key` header. Redirects are rejected. JSON
contains `action`, `item` (WIP and completed stage evidence), `parameters` (the
specific SOP selections), and `idempotency_key`. The gateway must return HTTP 200
with an action response object. It must store mutation outcomes durably by key
and return the same outcome if that key is replayed.

| Action | Required response |
| --- | --- |
| `contract` | `signed_contract_path`, `proposal_number`, `client_email`, `customer_name`, `amount`, `currency`, `start_date`, `line_item_id`, `work_order_address`, `tax_rate_area`, `tax_rate`; `po_number` and `po_address` when applicable, otherwise `comments` |
| `create_charges` | `invoice_number`, `amount` |
| `send_charges` | `sent: true` |
| `charge_status` | `ready: boolean`; when ready, `status: "Handled"` |
| `flex` | `order_number`, `proposal_number`, `billing_address`, `tax_rate_area` |
| `request_draft`, `request_final` | `job_id` |
| `draft_reports`, `final_reports` | `ready: boolean`; when ready, `reports` containing `Sales Order` and `EDI Invoice Extraction` |
| `download_draft`, `download_final` | `path`, `amount` (net), `net_amount`, `tax_amount`, `gross_amount`, `tax_rate`, `customer_name`, `audit_dates`, `currency`, `contract_number`, `order_number`, `billing_address`, `tax_rate_area` |
| `send_email` | `message_id`, `recipient` matching the signed contract |

Paths must point to files readable on the Veritas host through an approved
shared artifact location. A remote gateway returning its own private filesystem
paths will fail validation. The gateway must download the signed contract and
invoice PDFs, parse/verify their fields, and use the supplied final artifact hash
when delivering. Report polling must be scoped to the request job and order.
Tax area and address checks compare independently verified gateway facts; this
code does not supply a jurisdictional tax rules database.

A timeout or malformed response enters `reconciliation_required`. Inspect the
gateway's durable result for the event's idempotency key, save the verified
response to JSON, then record the reconciliation without replaying the action:

```powershell
.venv\Scripts\python.exe -m veritas.invoice_cli --mode live reconcile RUN_ID ITEM_ID verified-response.json --reviewer "Reviewer name"
```

The HTTP equivalent is `POST /api/invoicing/RUN_ID/reconcile` with `item_id`,
`response`, and `reviewer`. This advances one confirmed stage; call `advance`
afterward. CLI/API operations use a filesystem process lock. Direct Python
`InvoiceWorkflow` callers must serialize access across processes. Review
approvals apply to all items currently awaiting review in the run; inspect every
pending artifact first.

New runs carry the process version `meeting-2026-04-02`. Previously created runs
retain their original validation contract; create a new run in a separate demo
workspace to exercise the new walkthrough requirements. Do not migrate active
financial runs by editing their saved JSON.
