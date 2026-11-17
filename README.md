# Demeisen platform (v2)

The Demeisen intranet: a portal hosting separate functions, plus the front door that serves the platform and the
CRM at one address.

| Folder | What it is | Owns these tables in `intranet` |
|---|---|---|
| `portal/` | sign-in, home, people, audit, files; shared tools (`core.py`, `doctools.py`) | `users`, `audit` |
| `mnda/` | Mutual NDAs (oneNDA v2.1): Word + wordmarked PDF, status workflow, executed copies | `ndas` |
| `memo/` | Expenditure memos: every member signs off, printable record | `memos`, `signoffs` |
| `documents/` | Prospect-named documents, issue register, Verify | `issues` |
| `data/` | **data stack** `demeisen-v2-platform-data`: private network, RDS instance, files bucket, write-once audit bucket, AWS Backup | |
| `infra/` | **app stack** `demeisen-v2-platform-app`: the three functions and the CloudFront front door | |
| `ops/` | one-time set-up, copying v1, database bootstrap, and `RUNBOOK.md` | |

A function imports only from `portal` (never from another function) and plugs in through `register(registry)`:
menu entries, home tiles and cards, lookups for Verify, and its own migrations. A test fails if a function's SQL
touches another function's tables. To leave a function out, drop it from `PLATFORM_FUNCTIONS`.

The database is v1's `intranet`, unchanged: same tables, rows and IDs.

## Run locally

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m portal.app          # http://localhost:5000, data in ./local-data (SQLite)
.venv/bin/python -m pytest -q tests
```

Members from `portal/config.py` are created with password `changeme`. LibreOffice (`soffice`) is needed for PDFs.

## On AWS

- One image, three functions: `demeisen-v2-platform` (the app, in the private network, no internet),
  `demeisen-v2-platform-signin` (`/auth/*`, outside it so it can reach Cognito) and `demeisen-v2-platform-admin`
  (migrations and admin tasks, invoked only with AWS credentials).
- The app signs in to MySQL as `platform_app` with an IAM token: no stored database password. It can add to the audit
  trail but never change it; each audited action is also written once to the audit bucket (S3 Object Lock, 7 years).
- Only requests through the front door are served (a secret header added by CloudFront).
- Sign-in: the shared Demeisen pool (`demeisen_intranet_v2_auth`); a person is admitted only if active in `users`.

## Deploying

- **App:** every merge to `main` (workflow **Deploy**): image, app stack, migrations, front door address in SSM.
- **Data:** only by hand, workflow **Deploy data**, after approval in the GitHub `data` environment.
- First set-up, the test copy and cut-over: `ops/RUNBOOK.md`.
