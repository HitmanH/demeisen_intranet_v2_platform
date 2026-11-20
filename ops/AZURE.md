# Demeisen v2 on Azure

**Azure is the live system since the cut-over on 2026-11-20 (about 01:08 UTC):**
https://demeisen-v2-ddacbfayfrb7g9ge.z03.azurefd.net (intranet at `/`, CRM at `/crm/`).
The old AWS address (d1o33fjv6i8x8t.cloudfront.net) redirects there. AWS v2 is frozen, unchanged, as the fallback.
Sign-in stays on the Demeisen Cognito pool (username, password, 2FA) by decision; it is the one AWS piece kept.

Everything is in resource group `Non_Inv` (uksouth) and holds no data except the two MySQL servers, the storage
account and Key Vault. All templates are in this repo
(`infra/azure/`) or the CRM repo (`infra/azure/app.bicep`); each can be re-run.

| Step | What | Where | Status |
|---|---|---|---|
| 1 | Network, private links to MySQL, registry, Container Apps environment, app identities, storage | `infra/azure/foundations.bicep` | done |
| 2 | GitHub deploy identities (OIDC, main branch / `data` environment only) | `infra/azure/deploy-identities.bicep` | done |
| 3 | Database logins `*_app` / `*_migrator`, passwords only in Key Vault; CRM migration history | Cloud Shell block | done |
| 4 | Stored files (S3 `migration/` package) copied to the `files` container | Cloud Shell block | done |
| 5 | Code runs on both clouds (see below) | both repos | done |
| 6 | Container apps and jobs | `crm/infra/azure/app.bicep`, `infra/azure/app.bicep` | done |
| 7 | Front door (one address: `/`, `/crm/`, `/auth/`), `X-Origin-Verify` on both apps | `infra/azure/frontdoor.bicep` | done |
| 7a | Cognito: the v2 sign-in clients also accept the Azure address (SSM `/demeisen/v2/azure/url`, read by both AWS app stacks) | AWS app templates | done |
| 8–9 | Parallel test; cut-over (AWS redirect on, final copy, every table's row count matched) | `ops/azure/` | done 2026-11-20 |
| 10 | Retire AWS v2 and v1, keeping only the Cognito sign-in | | after 2–4 weeks |
| 11 | Microsoft (Entra) sign-in | not planned: decided to keep Cognito for now | |

## How the code tells the clouds apart
- **Database**: `DB_PASSWORD` / `DB_MIGRATOR_PASSWORD` set (from Key Vault) = password sign-in with public CA
  certificates; unset = AWS IAM token with the RDS certificate.
- **Files and audit copy**: `AZURE_STORAGE_ACCOUNT` set = Azure Blob (`files`, `audit`, `crm-backups`), signed in with
  the app's managed identity; otherwise S3.
- **Platform web**: `PLATFORM_CLOUD=azure` (set in `Dockerfile.azure`) refuses to start without shared sign-in and a
  real session secret, so built-in passwords can never be live on Azure.
- **CRM API**: on Azure, `src/server.mjs` checks the Cognito ID token itself (issuer, audience, signature, expiry),
  as API Gateway's JWT authorizer does on AWS.
- Admin tasks: platform `python -m portal.admin migrate|status|list_ndas`; CRM `node src/job.mjs migrate|status|backup`.

## Fallback to AWS v2 (if ever needed)
1. `aws ssm put-parameter --name /demeisen/v2/redirect-to-azure --value no --overwrite`, then run the platform's AWS
   **Deploy** workflow: the AWS address serves AWS v2 again (with its data as of the cut-over).
2. Anything entered on Azure since the cut-over is not on AWS: copy it back or re-enter it.

## Copying AWS v2 into Azure again (scripts in `ops/azure/`)
- AWS CloudShell: `bash export-v2-for-azure.sh start`, then `finish`; bundle the two dumps with the files and audit
  buckets into one `bundle.tar` in the transfer bucket and presign it (see the cut-over notes in git history).
- Azure Cloud Shell: save the link to `~/.cutover-url`, then `nohup bash cutover-azure.sh > ~/cutover.log 2>&1 &`.
  It saves the current Azure databases first (into the `files` container, `cutover-backup/`), loads, compares every
  table's row count, copies files and audit records. The MySQL servers' CanNotDelete lock also blocks deleting
  firewall rules, so the script lifts it for a moment and always restores it.

## Cut-over switch (AWS)
`/demeisen/v2/redirect-to-azure` = `yes` makes the AWS v2 front door send every request to the Azure address
(`/demeisen/v2/azure/url`); `no` turns AWS v2 back on. Applied by the platform's AWS Deploy workflow.

## Audit copy
Azure container `audit-worm`: version-level immutability, 7-year retention policy (unlocked; locking is irreversible
and is a separate decision). The app writes there via `AUDIT_CONTAINER`.

## Still to do
- Lock the audit container's retention policy (irreversible) once you're happy with 7 years.
- Database servers: public network access is on but with no firewall rules (closed); can be switched off entirely.
