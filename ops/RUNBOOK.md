# Demeisen v2: set-up, test copy and cut-over

Everything runs in **AWS CloudShell** (browser) or **GitHub Actions**. Nothing needs a local PC, and no AWS keys are
stored anywhere: GitHub deploys with short-lived roles (OIDC).

v1 keeps running, unchanged, until step 10. The v1 database is only ever read (snapshots).

## What goes where

| Repo | Stacks | Holds |
|---|---|---|
| `demeisen_intranet_v2_auth` | `demeisen-v2-auth-refs`, later `demeisen-v2-auth-pool` | the sign-in pool (your 2 users, passwords, 2FA) |
| `demeisen_intranet_v2_ui` | none | shared look |
| `demeisen_intranet_v2_crm` | `demeisen-v2-crm-data`, `demeisen-v2-crm-app` | CRM RDS instance + backups; CRM app |
| `demeisen_intranet_v2_platform` | `demeisen-v2-platform-data`, `demeisen-v2-platform-app`, `demeisen-v2-bootstrap` | platform RDS instance, files, audit trail; portal, MNDA, memos, documents; the front door |

Two RDS instances, each in its own private network: `crm` on one, `intranet` on the other. Neither app can reach
the other's database.

## 1. One-time AWS set-up (CloudShell, ~5 minutes)

```bash
git clone https://github.com/HitmanH/demeisen_intranet_v2_platform.git
git clone https://github.com/HitmanH/demeisen_intranet_v2_auth.git
cd demeisen_intranet_v2_platform && ./ops/setup.sh <your alert email>
```

It asks before each step:

1. **Protects v1's sign-in pool:** the v1 CRM stack will keep the pool, its domain and the GitHub OIDC provider even
   if that stack is deleted; deletion protection on the pool (currently off); termination protection on v1's stacks.
   It shows the change first and stops if anything would be replaced. No data changes.
2. **Bootstrap stack:** deploy roles, image repository, two generated secrets, backup role, cost alert.
3. **Pointers:** where v2 finds the pool (the existing one) and placeholder front-door parameters.

## 2. GitHub settings (browser, per v2 repo: auth, crm, platform)

- Settings → Secrets and variables → Actions → **Variables** → `AWS_ACCOUNT_ID` = `642552605309`
- Settings → Environments → **New environment** `data` → Required reviewers: you. Data workflows wait for your approval.

## 3. Copy v1 data (CloudShell)

```bash
./ops/copy-from-v1.sh
```

Prints a snapshot name such as `demeisen-v2-from-v1-20261117-1530`.

## 4. Create the two v2 databases (GitHub Actions, needs your approval)

In **both** `demeisen_intranet_v2_crm` and `demeisen_intranet_v2_platform`: Actions → **Deploy data** → Run workflow:
phase `test`, source_snapshot = the name from step 3, admin_endpoint `on`, alert_email = yours. Approve. ~15 minutes.

Then copy the files (the v2 files bucket now exists): `./ops/copy-from-v1.sh` again, or just the `aws s3 sync` line it prints.

## 5. Deploy the apps (GitHub Actions)

1. `demeisen_intranet_v2_platform` → Actions → **Deploy** → Run. The summary shows the **new address**
   (`https://dxxxx.cloudfront.net/`).
2. `demeisen_intranet_v2_crm` → Actions → **Deploy** → Run.
3. `demeisen_intranet_v2_platform` → **Deploy** again, which adds the CRM's routes (`/crm/`) to the front door.

(Merges to `main` deploy automatically from now on.)

## 6. Set up the two databases (CloudShell)

```bash
./ops/bootstrap-databases.sh
```

- CRM server: keeps `crm` exactly as it is, removes the copied `intranet` database, marks the 9 existing
  migrations as done (none runs again), adds the IAM-only `crm_migrator` login.
- Platform server: keeps `intranet` exactly as it is (same tables, rows, IDs; one small `schema_migrations` table is
  added), removes the copied `crm` database and v1's password logins, adds the IAM-only `platform_app` and
  `platform_migrator` logins.

## 7. Close the admin door (GitHub Actions)

Both repos: **Deploy data** with admin_endpoint `off`.

## 8. Try v2

Open the new address. Sign in with your usual email, password and authenticator code. The test copy is real data,
but changes made in v2 now are **not** carried back to v1 and will be overwritten at cut-over.

## 9. Cut-over (when you're happy)

1. Tell users to stop using v1 (both v1 addresses).
2. `./ops/copy-from-v1.sh` → new snapshot name.
3. Both repos: **Deploy data** with source_snapshot = the new name, admin_endpoint `on` (still phase `test`; this
   replaces the test copies).
4. Both repos: **Deploy** (the apps pick up the new database addresses), then `./ops/bootstrap-databases.sh`.
5. `aws s3 sync` the files again (from copy-from-v1.sh's output).
6. Both repos: **Deploy data** with phase **`live`**, admin_endpoint `off`. From now on: deletion protection on, and
   the stack policy refuses anything that would replace or delete the database or buckets.
7. Pool invitation emails: `demeisen_intranet_v2_auth` → **Deploy pool** (site_url blank = the new address).

## 10. Retire v1 (weeks later, once v2 has run cleanly)

1. Final snapshot of v1: `aws rds create-db-snapshot --db-instance-identifier demeisen-crm-poc --db-snapshot-identifier demeisen-v1-final`
2. Release the pool from v1, then adopt it into v2:
   - Delete the v1 stack `demeisen-intranet-signin` (only the v1 intranet's sign-in client).
   - Turn off termination protection on `demeisen-crm-poc` and delete it. Because of step 1.1, the pool, its domain,
     the GitHub OIDC provider and the old backup bucket are **kept**. The v1 RDS instance was never in that stack,
     so it's kept too.
   - `cd demeisen_intranet_v2_auth && ./pool/import.sh` adopts the pool into `demeisen-v2-auth-pool`.
3. v1 intranet: delete the v1 Lambda functions, CloudFront distribution, ECR repository and CodeBuild project
   (they were created by scripts, not a stack). Keep the v1 files bucket until you no longer need it.
4. Stop, then (when sure) delete the v1 RDS instance `demeisen-crm-poc`. Turn off its deletion protection first;
   it takes a final snapshot.

## Restore live data

- **A mistake in the last 35 days:** RDS console → the v2 instance → Actions → Restore to point in time → new
  instance. Copy the rows you need across, or snapshot it and use the procedure below.
- **Whole database:** take or choose a snapshot (AWS Backup vault keeps one a day for a year), then, because live
  data can't be replaced by the workflow: CloudShell → `aws cloudformation set-stack-policy` to allow replacement on
  `Db` for this one update, **Deploy data** with the snapshot, re-apply the policy (the workflow does this). Then
  **Deploy** both apps.
- **Files:** the files bucket is versioned: restore an earlier version in the S3 console.
- **Audit trail:** the write-once copy is in `demeisen-v2-platform-audit-<account>` under `audit/YYYY/MM/DD/`.

## Costs (approximate)

Two `db.t4g.micro` instances with 20 GiB gp3: about $30 a month together. AWS Backup snapshots, S3, Lambda,
CloudFront: a few dollars. The admin endpoint costs about $0.01 an hour, only while on. During the test period v1
also runs, so the bill is higher until step 10. A cost alert goes to your email at 80% of $120 a month.
