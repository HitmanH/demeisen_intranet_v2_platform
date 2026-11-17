# demeisen_intranet_v2_platform: notes for Claude sessions

Portal + functions (mnda, memo, documents) + the front door. Read README.md and ops/RUNBOOK.md first.

## Rules
- Data lives in `data/data.yaml` (stack `demeisen-v2-platform-data`). Never remove `DeletionPolicy`,
  `DeletionProtection`, encryption, versioning, Object Lock, backups or the stack policy;
  `python3 tools/check-guardrails.py data/data.yaml` must pass.
- The database is v1's `intranet`, kept identical. Schema changes: add a migration to the owning function's
  MIGRATIONS (never edit one that has run). A function never touches another function's tables (a test checks).
- Never change `SourceSnapshot` once Phase is `live`. Never switch AuditLockMode to COMPLIANCE without Russell's
  explicit decision: it cannot be undone.
- Never touch the CRM's database or stacks from here; the CRM is its own repo (demeisen_intranet_v2_crm).
- Data changes only via the **Deploy data** workflow (environment `data`, Russell approves). App deploys on merge to
  main. Nothing from a laptop; no AWS keys in the repo or CI secrets.
- Admin tasks: `aws lambda invoke --function-name demeisen-v2-platform-admin --payload '{"admin_task":"status"}'`
  (see portal/lambda_handler.py).
- AWS: account 642552605309, eu-west-2.

## History
v1 was the intranet POC (repo -demeisen-intranet-poc): Lightsail, then Lambda created by scripts, sharing an RDS
server with the CRM. v2 (Nov 2026) split the code into folders, gave the platform its own RDS instance and network,
moved all infrastructure into CloudFormation and all deploys into GitHub Actions.
