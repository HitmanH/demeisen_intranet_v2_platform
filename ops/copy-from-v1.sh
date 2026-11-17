#!/usr/bin/env bash
# Copies v1's data for v2, without changing v1. Run in AWS CloudShell. Used twice: once to try v2, and once at cut-over.
#   ./ops/copy-from-v1.sh
# 1. Takes a manual snapshot of the v1 database server (demeisen-crm-poc). Reading only: v1 keeps running unchanged.
#    Both v2 databases start from this one snapshot; each then removes the other app's database (bootstrap step).
# 2. Copies the v1 intranet files (NDAs, PDFs, uploads) into the v2 files bucket. Nothing is deleted on either side.
# Prints the snapshot name to use as source_snapshot in the two "Deploy data" workflows.
set -euo pipefail
export AWS_REGION="${AWS_REGION:-eu-west-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
ACCT=$(aws sts get-caller-identity --query Account --output text)
SNAP="demeisen-v2-from-v1-$(date -u +%Y%m%d-%H%M)"
V1_FILES="demeisen-intranet-files-$ACCT"; V2_FILES="demeisen-v2-platform-files-$ACCT"

echo "1. Snapshot of demeisen-crm-poc -> $SNAP (a few minutes; v1 stays online)"
aws rds create-db-snapshot --db-instance-identifier demeisen-crm-poc --db-snapshot-identifier "$SNAP" \
  --tags Key=demeisen:purpose,Value=v2-copy >/dev/null
aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP"
aws rds describe-db-snapshots --db-snapshot-identifier "$SNAP" --query 'DBSnapshots[0].{snapshot:DBSnapshotIdentifier,engine:EngineVersion,encrypted:Encrypted,sizeGiB:AllocatedStorage,taken:SnapshotCreateTime}' --output table

if aws s3api head-bucket --bucket "$V2_FILES" 2>/dev/null; then
  echo "2. Files: s3://$V1_FILES -> s3://$V2_FILES (copy only, nothing deleted)"
  aws s3 sync "s3://$V1_FILES" "s3://$V2_FILES" --only-show-errors
  echo "   v1: $(aws s3 ls "s3://$V1_FILES" --recursive | wc -l) objects, v2: $(aws s3 ls "s3://$V2_FILES" --recursive | wc -l) objects"
else
  echo "2. The v2 files bucket doesn't exist yet: run the platform's Deploy data first, then run this script again"
  echo "   (or just: aws s3 sync s3://$V1_FILES s3://$V2_FILES)."
fi
echo
echo "Snapshot for both Deploy data workflows (source_snapshot): $SNAP"
