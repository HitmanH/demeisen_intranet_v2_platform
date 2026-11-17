#!/usr/bin/env bash
# One-time set-up for Demeisen v2. Run in AWS CloudShell (eu-west-2), from a checkout of demeisen_intranet_v2_platform:
#   git clone https://github.com/HitmanH/demeisen_intranet_v2_platform.git && cd demeisen_intranet_v2_platform
#   ./ops/setup.sh you@example.com
# Safe to re-run. It changes nothing in v1's data. Steps (each shows what it will do and asks first):
#   1. Protects v1's sign-in pool: makes the v1 CRM stack keep the pool, its domain and the GitHub OIDC provider even
#      if that stack is deleted, and turns on the pool's deletion protection. Termination protection on v1's stacks.
#   2. Deploys ops/bootstrap.yaml (deploy roles, image repository, secrets, backup role, cost alert).
#   3. Points v2 at the existing pool (stack demeisen-v2-auth-refs) and creates placeholder front-door parameters.
set -euo pipefail
EMAIL="${1:?Usage: ./ops/setup.sh <alert email>}"
export AWS_REGION="${AWS_REGION:-eu-west-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
V1=demeisen-crm-poc; POOL=eu-west-2_o84PDILyl
here="$(cd "$(dirname "$0")" && pwd)"; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
ask() { read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
ACCT=$(aws sts get-caller-identity --query Account --output text)
echo "Account $ACCT, region $AWS_REGION"

# ---------- 1. Protect v1's sign-in pool ----------
aws cloudformation get-template --stack-name "$V1" --query TemplateBody --output text > "$tmp/v1.yaml"
python3 - "$tmp/v1.yaml" "$tmp/v1-protected.yaml" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
def retain(s, logical, typ):
    head = f"  {logical}:\n    Type: {typ}\n"
    if head not in s: sys.exit(f"{logical} not found in the v1 template; stopping without changes")
    after = s.split(head, 1)[1]
    if after.startswith("    DeletionPolicy: Retain"): return s
    return s.replace(head, head + "    DeletionPolicy: Retain\n    UpdateReplacePolicy: Retain\n", 1)
s = retain(s, "UserPool", "AWS::Cognito::UserPool")
s = retain(s, "UserPoolDomain", "AWS::Cognito::UserPoolDomain")
s = retain(s, "GitHubOidcProvider", "AWS::IAM::OIDCProvider")
if "DeletionProtection: ACTIVE" not in s:
    s = s.replace("      UserPoolName: demeisen-crm-poc\n", "      UserPoolName: demeisen-crm-poc\n      DeletionProtection: ACTIVE\n", 1)
open(sys.argv[2], "w").write(s)
PY
if cmp -s "$tmp/v1.yaml" "$tmp/v1-protected.yaml"; then
  echo "1. v1 already protected."
else
  diff "$tmp/v1.yaml" "$tmp/v1-protected.yaml" || true
  if ask "1. Apply these protections to $V1? (no resource is replaced; data untouched)"; then
    params=$(aws cloudformation describe-stacks --stack-name "$V1" --query 'Stacks[0].Parameters[].ParameterKey' --output text | tr '\t' '\n' | sed 's/.*/ParameterKey=&,UsePreviousValue=true/' | tr '\n' ' ')
    # shellcheck disable=SC2086
    aws cloudformation create-change-set --stack-name "$V1" --change-set-name protect-for-v2 --template-body "file://$tmp/v1-protected.yaml" \
      --capabilities CAPABILITY_NAMED_IAM --parameters $params >/dev/null
    aws cloudformation wait change-set-create-complete --stack-name "$V1" --change-set-name protect-for-v2 || true
    aws cloudformation describe-change-set --stack-name "$V1" --change-set-name protect-for-v2 \
      --query '{status:Status,reason:StatusReason,changes:Changes[].ResourceChange.[Action,LogicalResourceId,Replacement]}' --output json
    if aws cloudformation describe-change-set --stack-name "$V1" --change-set-name protect-for-v2 --query 'Changes[].ResourceChange.Replacement' --output text | grep -q True; then
      echo "The change would replace a resource. Stopping; nothing applied."; exit 1
    fi
    if ask "   Execute?"; then
      aws cloudformation execute-change-set --stack-name "$V1" --change-set-name protect-for-v2
      aws cloudformation wait stack-update-complete --stack-name "$V1"
    else aws cloudformation delete-change-set --stack-name "$V1" --change-set-name protect-for-v2; fi
  fi
fi
for s in "$V1" demeisen-intranet-signin; do aws cloudformation update-termination-protection --stack-name "$s" --enable-termination-protection >/dev/null && echo "   termination protection on: $s"; done
aws cognito-idp describe-user-pool --user-pool-id "$POOL" --query 'UserPool.{pool:Name,deletionProtection:DeletionProtection,users:EstimatedNumberOfUsers}' --output table

# ---------- 2. Bootstrap stack ----------
if ask "2. Deploy demeisen-v2-bootstrap (deploy roles, image repository, secrets, backup role, cost alert to $EMAIL)?"; then
  aws cloudformation deploy --stack-name demeisen-v2-bootstrap --template-file "$here/bootstrap.yaml" --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides AlertEmail="$EMAIL" OidcProviderArn="arn:aws:iam::$ACCT:oidc-provider/token.actions.githubusercontent.com" --no-fail-on-empty-changeset
  aws cloudformation update-termination-protection --stack-name demeisen-v2-bootstrap --enable-termination-protection >/dev/null
fi

# ---------- 3. Where v2 finds the sign-in pool and the front door ----------
if [ -f "$here/../../demeisen_intranet_v2_auth/pool/refs.yaml" ]; then REFS="$here/../../demeisen_intranet_v2_auth/pool/refs.yaml"
else curl -fsSL https://raw.githubusercontent.com/HitmanH/demeisen_intranet_v2_auth/main/pool/refs.yaml -o "$tmp/refs.yaml"; REFS="$tmp/refs.yaml"; fi
aws cloudformation deploy --stack-name demeisen-v2-auth-refs --template-file "$REFS" --parameter-overrides UserPoolId="$POOL" --no-fail-on-empty-changeset
for kv in "/demeisen/v2/edge/origin=https://example.invalid" "/demeisen/v2/edge/distribution-arn=none"; do
  aws ssm get-parameter --name "${kv%%=*}" >/dev/null 2>&1 || aws ssm put-parameter --name "${kv%%=*}" --type String --value "${kv#*=}" >/dev/null
done
echo
echo "Done. Next (ops/RUNBOOK.md, step 2): in GitHub, for each of the four v2 repos, add the repository variable"
echo "AWS_ACCOUNT_ID=$ACCT, and create the environment 'data' with yourself as required reviewer."
