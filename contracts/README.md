# Contract review (`contracts/`)

**mycroft.demeisen.io/contracts**: upload a PDF or Word document, pick a category (NDA / confidentiality,
professional services engagement, vendor agreement (data / tech), trial / pilot, other), optionally name the
counterparty and give some context. Claude reads the whole document and the page shows:

- an overview (parties, our position, dates, law and courts, overall risk, a one-line recommendation)
- terms to push back on: high / medium / low, each with why it matters and the change to ask for, plus missing protections
- money and liability: fees, payment terms, caps, exclusions, indemnities
- term and termination: renewal, notice, what survives, dates to diary
- key terms
- drafting errors: broken cross-references, numbering, defined terms, wrong party, etc.

The category steers what Claude looks for (see `_CATEGORY_FOCUS` in `review.py`; edit it to add house views).
Word files are converted to PDF with LibreOffice first so Claude sees the numbering as printed.

## How it runs
- Upload: the original is stored (`uploads/` in the files store), a row is added to `contracts`, and the review starts
  on a background thread. The page refreshes every few seconds until it is done (usually 1 to 3 minutes).
- One request to the Claude API (`claude-opus-5-5`, effort `high`, structured JSON output, server-side refusal
  fallback). Override with `CONTRACTS_MODEL` / `CONTRACTS_EFFORT`.
- Failures (rate limit, network, unreadable file) are shown on the page with *Run review* to retry. A review still
  "running" after 20 minutes is treated as interrupted (e.g. the app restarted) and can be re-run.
- Everything is in the audit trail (uploaded, details changed, reviewed, re-run).
- Cost: roughly 3,000 input tokens per page plus the answer; a 10-page agreement is roughly $0.50 on Opus 5.5 at the
  published rates.

## Switching it on (once)
Without the key, uploads are kept and the page says reviews are not switched on yet.

1. Create an API key at console.anthropic.com (ideally in its own workspace with a monthly spend limit).
2. Azure Cloud Shell, put it in Key Vault (prompted, so it isn't left in the shell history):
   ```
   read -rs KEY && az keyvault secret set --vault-name <demeisen-kv-xxxxxx> -n anthropic-api-key --value "$KEY" -o none; unset KEY
   ```
3. Give it to the web app:
   ```
   KV=$(az keyvault show -n <demeisen-kv-xxxxxx> --query properties.vaultUri -o tsv)
   ID=$(az identity show -g Non_Inv -n demeisen-v2-platform --query id -o tsv)
   az containerapp secret set -n demeisen-v2-platform -g Non_Inv --secrets "anthropic-api-key=keyvaultref:${KV}secrets/anthropic-api-key,identityref:$ID"
   az containerapp update -n demeisen-v2-platform -g Non_Inv --set-env-vars ANTHROPIC_API_KEY=secretref:anthropic-api-key
   ```
   When `infra/azure/app.bicep` is next re-run, pass `contractReview=true` so the key stays.

## Confidentiality
Documents are sent to Anthropic's API to be reviewed. Under Anthropic's commercial terms, API inputs are not used to
train models; they are kept only for a limited period. Zero data retention is available from Anthropic by
agreement. Check that sharing a counterparty's document this way is allowed by any NDA that covers it.

AWS v2 (Lambda) is only a fallback: background reviews need the Azure container.
