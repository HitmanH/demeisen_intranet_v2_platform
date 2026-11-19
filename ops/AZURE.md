# Demeisen v2 on Azure

AWS v2 stays live until cut-over. Everything here runs alongside it, in resource group `Non_Inv` (uksouth), and
holds no data except the two MySQL servers, the storage account and Key Vault. All templates are in this repo
(`infra/azure/`) or the CRM repo (`infra/azure/app.bicep`); each can be re-run.

| Step | What | Where | Status |
|---|---|---|---|
| 1 | Network, private links to MySQL, registry, Container Apps environment, app identities, storage | `infra/azure/foundations.bicep` | done |
| 2 | GitHub deploy identities (OIDC, main branch / `data` environment only) | `infra/azure/deploy-identities.bicep` | done |
| 3 | Database logins `*_app` / `*_migrator`, passwords only in Key Vault; CRM migration history | Cloud Shell block | done |
| 4 | Stored files (S3 `migration/` package) copied to the `files` container | Cloud Shell block | done |
| 5 | Code runs on both clouds (see below) | both repos | done |
| 6 | Container apps and jobs | `crm/infra/azure/app.bicep`, `infra/azure/app.bicep` | |
| 7 | Front door (one address: `/`, `/crm/`, `/auth/`) | to do | |
| 7a | Cognito: allow the Azure address to sign in | to do (with sign-in) | |
| 8–10 | Parallel test, cut-over (re-copy data), retire AWS | | |
| 11 | Microsoft (Entra) sign-in instead of Cognito | later | |

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

## Still to do
- Audit copy: the `audit` container is not yet write-once (AWS uses S3 Object Lock). Add version-level immutability
  before cut-over.
- Platform web app: needs a Cognito app client for the Azure address and Key Vault secrets
  `platform-oidc-client-secret`, `platform-flask-secret`, then `app.bicep enableWeb=true` (with step 7a).
- Front door with the `X-Origin-Verify` header, then `ORIGIN_VERIFY` on both apps.
- Database servers: turn off public network access once everything uses the private links.
