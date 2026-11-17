#!/usr/bin/env bash
# One-time set-up of the two v2 databases after they were created from a v1 snapshot. Run in AWS CloudShell, after
# both data stacks were deployed with admin_endpoint=on and both app stacks were deployed (RUNBOOK.md step 6).
#   ./ops/bootstrap-databases.sh
# CRM server:      keeps database `crm` exactly as it is; removes the copied `intranet` database and its login; records
#                  the 9 existing migrations as already applied (none of them runs again); adds crm_migrator.
# Platform server: keeps database `intranet` exactly as it is; removes the copied `crm` database and v1 logins; adds
#                  platform_migrator and platform_app, then records the platform's tables as present.
# Only the v2 copies are changed. v1 is never touched.
set -euo pipefail
export AWS_REGION="${AWS_REGION:-eu-west-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
call() {  # function payload
  aws lambda invoke --function-name "$1" --cli-binary-format raw-in-base64-out --cli-read-timeout 900 --payload "$2" /tmp/out.json >/dev/null
  cat /tmp/out.json; echo
  if grep -q '"error"\|errorMessage' /tmp/out.json; then echo "Stopped: see the message above."; exit 1; fi
}
ask() { read -r -p "$1 [y/N] " a; [ "$a" = y ]; }

BASELINE='["001_schema.sql","002_app_user.sql","003_counterparty_pairing.sql","004_pairing_detail.sql","005_person_contact_details.sql","006_engagement_stage.sql","007_introducer_types.sql","008_material_shared.sql","009_consultant_type.sql"]'
echo "CRM v2 database:"
aws lambda invoke --function-name demeisen-v2-crm-migrate --cli-binary-format raw-in-base64-out --payload '{"action":"status"}' /tmp/out.json >/dev/null || true
if grep -q '"applied"' /tmp/out.json; then echo "  already bootstrapped: $(cat /tmp/out.json)"
elif ask "  Remove the copied intranet database from the CRM server and set it up?"; then
  call demeisen-v2-crm-migrate "{\"action\":\"bootstrap\",\"baseline\":$BASELINE,\"dropDatabases\":[\"intranet\"],\"confirm\":\"intranet\"}"
  call demeisen-v2-crm-migrate '{"action":"migrate"}'
fi

echo "Platform v2 database:"
aws lambda invoke --function-name demeisen-v2-platform-admin --cli-binary-format raw-in-base64-out --payload '{"admin_task":"status"}' /tmp/out.json >/dev/null || true
if grep -q '"applied"' /tmp/out.json; then echo "  already bootstrapped: $(cat /tmp/out.json)"
elif ask "  Remove the copied crm database and v1 logins (intranet, crm_app) from the platform server and set it up?"; then
  call demeisen-v2-platform-admin '{"admin_task":"bootstrap","dropDatabases":["crm"],"dropUsers":["intranet","crm_app"],"confirm":"crm,intranet,crm_app"}'
  call demeisen-v2-platform-admin '{"admin_task":"migrate"}'
fi
echo
echo "Done. Now run both 'Deploy data' workflows with admin_endpoint=off (RUNBOOK.md step 7)."
