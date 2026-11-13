VERITAS - UC-based audit proposal extraction

SPECIAL AUDIT INVOICING
  Complete local assisted application: APPLICATION_DESIGN.md.
  Work queue, operator tasks, evidence library, exceptions, approvals, PDF filing,
  daily/monthly reporting and country configuration are available in the web UI.
  Start start-web.ps1 and open http://127.0.0.1:8765/#invoicing.
  Choose Demo for synthetic examples or Assisted for actual operator-recorded work.
  Connected mode requires an approved tested SmartCert/Flex/email gateway.
  See INVOICING.md for the Dubai agent workflow, CSV/XLSX intake, draft/final
  approvals, CLI, HTTP API and live gateway contract. Demo never sends email.
  .venv\Scripts\python.exe -m veritas.invoice_cli --mode demo create scripts\wip-demo.csv --month 2026-11
  Live Smart Cert/Flex connections require your portal/API configuration.

DEFAULT: local PDF text, Tesseract OCR and exact-document result cache.
Cloud fallback is OFF by default. Unresolved fields can be reviewed manually.

START
  powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
This opens the desktop review UI. No local language-model service is started.

REACT WEB UI
  powershell -NoProfile -ExecutionPolicy Bypass -File .\start-web.ps1
Open http://127.0.0.1:8765 in your browser. Node.js is needed for the first build.
Upload a PDF or ZIP (up to 100 MB), review fields and evidence, save corrections,
and download CSV/JSON. Web results are saved under outputs/web and reload on
restart. Extraction uses the existing local engine; cloud fallback stays off.
For frontend development: cd ui, npm.cmd run dev (Python server must also run).

CLI
  .venv\Scripts\python.exe -m veritas samples --output outputs\ocr-only
  .venv\Scripts\python.exe -m veritas samples --fallback azure --output outputs\azure
  .venv\Scripts\python.exe -m veritas samples --refresh --fallback azure --output outputs\fresh
--gpt is also supported for Azure fallback. --refresh bypasses extraction results,
not the local OCR cache. The same exact document can reuse a complete saved result
without new model calls. Human corrections update that result cache.

UI
Choose PDF, ZIP or folder. Select a result to inspect its five editable fields,
page evidence and warnings. Open original PDF checks the scan. Save reviewed
values writes JSON/CSV and preserves corrections. Load results reopens exports.
Azure/OpenAI provider choices are available; model fallback must be enabled
explicitly. Provider selection alone does not start a request.

AZURE SETTINGS
UC credentials and deployment are loaded in memory from
D:\python\UC\config\environment\.env.cdp
Override with VERITAS_UC_ENV_FILE. Existing environment values take precedence.
No keys are copied or displayed. Azure fallback sends selected document text
and images to that endpoint and may incur charges.

SETUP ON ANOTHER MACHINE (Python 3.11+ with Tkinter)
  uv venv
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt
Install Tesseract OCR with English language data. It is discovered on PATH or
C:\Program Files\Tesseract-OCR\tesseract.exe. Set TESSERACT_CMD for another path.

ECONOMY PIPELINE
Fields: audit_type, amount, man_days, proposal_number, client_email_id.
Local cover/contact rules preserve evidence and flag ambiguous OCR for review.
Only unresolved fields are sent to the enabled provider. Context is capped at
3,500 characters and two relevant images resized to 1,200 pixels maximum on
the longest side. Model responses have validated JSON schemas and bounded output.
Human review flags never automatically trigger a paid re-verification request.
A populated field is not necessarily verified. Check decimal points, email
punctuation, audit stages and years. Rubis's printed total has malformed digit
grouping. Dairy's quantity Unit cells are blank. Both need confirmation.

ACCOUNTING
results.json records usage per document. token_usage.json reports model tokens
and cloud_totals. Reusing results reports zero new tokens and preserves original
usage as metadata. Missing usage is explicitly unknown, not counted as zero.
Four-document test: prior full-document fallback 174,790 tokens; bounded fresh
fallback 8,911; repeat using exact-document cache zero. See
outputs/economy-comparison.json. This is not an unseen-document accuracy claim.

SOURCE
veritas/engine.py: PDF text, OCR, extraction, evidence and result cache
veritas/economy.py: local rules and context limits
veritas/provider.py: UC settings and Azure/OpenAI adapters
veritas/desktop.py: desktop UI
veritas/__main__.py: PDF/ZIP CLI and exports
veritas/uc/: reused hashing, storage, safe ZIP and quote verification functions
UC provenance is recorded in veritas/uc/provenance.json. D:\python\UC is unchanged.
Documents are data, not instructions. ZIP paths are validated before extraction.
Samples, outputs, environments and secrets are excluded from source control.

TESTS
  uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
  .venv\Scripts\python.exe -m pytest -q
