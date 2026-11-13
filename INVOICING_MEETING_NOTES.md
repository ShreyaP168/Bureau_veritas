# April 2, 2026 invoicing walkthrough

Historical source notes: later explicit requirements supersede the strict-month
filter and demo-only SA limitation. See `APPLICATION_DESIGN.md` for the current
application, backlog eligibility, assisted execution and configuration model.

Source: user-provided transcript, “Dubai & South Africa Invoicing
Process-20260402_112553-Meeting Recording 1 (2)”. This describes observed workflow
behavior, not an authoritative tax policy or a complete portal API specification.

| Time | Requirement applied |
| --- | --- |
| 0:03–2:16 | WIP not fully invoiced with filters; report preamble removal; Dubai country code AE |
| 3:30–4:52 | Signed contract under More > Documents; Work Orders date verification; Quotation remains an alternate route from the earlier SOP |
| 5:17–5:38 | Dubai no-PO charge comment contains the verified audit date, preserving other WIP comments |
| 9:39–10:15 | Billing address check; the Dubai example uses AE10; AE11 is mentioned for another location but is not assigned automatically |
| 11:09–12:07 | Validate customer, address, audit date, net amount, VAT and gross total; Dubai example uses 5% VAT |
| 17:00–17:17 | South Africa country code ZA; remove exact duplicate WIP records without collapsing distinct audit lines |
| 19:10–20:15 | South Africa on-site + reporting man-days and amounts reconcile with WIP; management fee is a separate charge line |
| 21:53–24:21 | Separate detail attachments for on-site, reporting and annual project management, preserving contract currency |
| 24:30–27:14 | Header contains audit type, standard, WIP Service Resource auditor, Work Order audit dates, and blank PO when absent |
| 27:31–28:58 | Standard Draft and Standard Invoice routes; country coordinator reviews the draft before final issuance |

## South Africa demo

```powershell
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo create scripts\wip-south-africa-demo.csv --month 2026-04 --country ZA
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo advance RUN_ID
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo approve RUN_ID --reviewer "Demo Coordinator" --reviewer-role country_coordinator
.venv\Scripts\python.exe -m veritas.invoice_cli --mode demo approve RUN_ID --reviewer "Demo Final Reviewer"
```

The fixture uses synthetic names and standard. It demonstrates 3.5 on-site plus
1.5 reporting days at USD 1,000 per day, and a USD 520 management fee: USD 5,000
base audit plus USD 520 fee = USD 5,520 net. Its zero demo tax rate is explicitly
synthetic and is **not a South Africa tax rule**. No invoice is issued or emailed.

SA demo WIP requires Currency, Man Days and Service Resource. To construct the
synthetic signed-contract response it also needs Standard Name, semicolon-separated
ISO Audit Dates, On Site Man Days, Reporting Man Days, Unit Price, Annual Project
Management Fee and Demo Tax Rate. Missing values block progression. Production
contract facts must be extracted from signed documents, not defaulted from this
fixture. The demo models Surveillance Audit 1/SV1 only; other SA audit types are
excluded pending confirmation.

The gateway contract adds `charge_lines` (unique line IDs, kind, description,
currency, amount, and man-days/unit price for on-site/reporting), `standard_name`,
`auditor_name`, `audit_dates` and a structured `header_attachment`. Flex must echo
that header and the verified lines as `detail_attachments`. The exact official
rendering templates remain unavailable. Currency is checked per line; fees are
included in charge and invoice totals, while WIP audit amount/man-days reconcile
against the on-site and reporting lines. Conflicting duplicate rows stop intake.

CLI `--reviewer-role country_coordinator` or HTTP `reviewer_role` records the local
role attestation. This does not authenticate employment or grant enterprise access.

## Items that still require confirmation

- The transcript says both “incomplete” and “completed”; the original SOP's
  Completed, Confirmed and Frozen Dubai filter is retained.
- It describes April processing but demonstrates a March 25 Work Order. The
  selected-month filter remains strict; prior-month/backlog billing is not
  silently enabled.
- South Africa business units, full status/audit eligibility, production tax
  treatment, attachment wording/templates and billing-address exceptions are not
  fully specified. Live SA processing remains disabled.
- USD rates and the management fee are example contract values, not global rates.
- The actual report selections and portal access still require Smart Cert/Flex
  URLs and API documentation or an approved browser automation integration.

Financial arithmetic uses Decimal with half-up rounding to two decimal places.
Jurisdictions/currencies requiring another rounding policy need a confirmed
configuration before production use. The Dubai walkthrough check applies AE10
and 5% only when the verified billing jurisdiction explicitly says Dubai; other
locations are not inferred from the country filter.
