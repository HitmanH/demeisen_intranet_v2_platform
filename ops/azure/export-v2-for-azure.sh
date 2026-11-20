#!/usr/bin/env bash
# Exports the AWS v2 databases (crm, intranet) for loading into Azure. Run in AWS CloudShell.
# Only READS v2: it snapshots each v2 database, restores each snapshot as a TEMPORARY copy reachable only from
# this CloudShell's address, dumps it, uploads the dump to a private S3 bucket with a 2-hour link, then deletes the
# temporary copies. The v2 snapshots are kept (labelled to-azure) as a record of exactly what was copied.
#   bash export-v2-for-azure.sh start     snapshots + temporary copies (about 15 minutes, can be left)
#   bash export-v2-for-azure.sh finish    dump, upload, print links, delete temporary copies
set -uo pipefail
export AWS_DEFAULT_REGION=eu-west-2 AWS_PAGER=""
ACCT=$(aws sts get-caller-identity --query Account --output text)
VPC=vpc-044079edc8236aae8                      # default VPC: public subnets, needed for a reachable temporary copy
BUCKET=demeisen-azure-transfer-$ACCT
STATE=~/.azure-export; mkdir -p "$STATE"; chmod 700 "$STATE"
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
db_of() { [ "$1" = crm ] && echo crm || echo intranet; }
src_of() { aws cloudformation describe-stack-resource --stack-name "demeisen-v2-$1-data" --logical-resource-id Db \
             --query StackResourceDetail.PhysicalResourceId --output text; }

start() {
  TS=$(date -u +%Y%m%d-%H%M); echo "$TS" > "$STATE/ts"
  MYIP=$(curl -s https://checkip.amazonaws.com); echo "$MYIP" > "$STATE/ip"
  SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values=demeisen-azure-export Name=vpc-id,Values=$VPC \
         --query 'SecurityGroups[0].GroupId' --output text)
  if [ "$SG" = None ]; then
    SG=$(aws ec2 create-security-group --group-name demeisen-azure-export --vpc-id $VPC \
           --description "Temporary: Azure export, this CloudShell only" --query GroupId --output text)
  fi
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 3306 --cidr "$MYIP/32" >/dev/null 2>&1 || true
  echo "$SG" > "$STATE/sg"
  for A in crm platform; do
    SRC=$(src_of $A); SNAP=demeisen-v2-$A-to-azure-$TS
    log "$A: snapshot of $SRC -> $SNAP"
    aws rds create-db-snapshot --db-instance-identifier "$SRC" --db-snapshot-identifier "$SNAP" \
      --tags Key=demeisen:purpose,Value=to-azure >/dev/null || { log "STOPPED: snapshot $A"; return 1; }
  done
  for A in crm platform; do
    SNAP=demeisen-v2-$A-to-azure-$TS
    aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP"; log "$A: snapshot ready"
    aws rds restore-db-instance-from-db-snapshot --db-instance-identifier "demeisen-azure-export-$A" \
      --db-snapshot-identifier "$SNAP" --db-instance-class db.t4g.micro --db-subnet-group-name default \
      --vpc-security-group-ids "$SG" --publicly-accessible --backup-retention-period 0 --no-multi-az \
      --tags Key=demeisen:purpose,Value=temporary-azure-export >/dev/null || { log "STOPPED: restore $A"; return 1; }
    log "$A: temporary copy demeisen-azure-export-$A is being created"
  done
  log "Started. In about 15 minutes run: bash export-v2-for-azure.sh finish"
}

finish() {
  TS=$(cat "$STATE/ts"); SG=$(cat "$STATE/sg")
  if ! command -v mysqldump >/dev/null; then
    log "installing the MySQL 8.4 client"
    sudo dnf -y -q install https://repo.mysql.com/mysql84-community-release-el9-1.noarch.rpm >/dev/null 2>&1 &&
      sudo dnf -y -q install mysql-community-client >/dev/null 2>&1 || sudo dnf -y -q install mariadb105 >/dev/null
  fi
  aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || {
    aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint=eu-west-2 >/dev/null
    aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
      '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":2}}]}'
  }
  for A in crm platform; do
    I=demeisen-azure-export-$A; D=$(db_of $A)
    aws rds wait db-instance-available --db-instance-identifier "$I"
    PW="$(openssl rand -base64 24 | tr -d '/+=')Aa1"
    aws rds modify-db-instance --db-instance-identifier "$I" --master-user-password "$PW" --apply-immediately >/dev/null
    sleep 60; aws rds wait db-instance-available --db-instance-identifier "$I"
    H=$(aws rds describe-db-instances --db-instance-identifier "$I" --query 'DBInstances[0].Endpoint.Address' --output text)
    U=$(aws rds describe-db-instances --db-instance-identifier "$I" --query 'DBInstances[0].MasterUsername' --output text)
    W=$(mktemp -d); export MYSQL_PWD="$PW"
    log "$A: dumping database $D"
    mysqldump -h "$H" -u "$U" --ssl --single-transaction --routines --triggers --events --no-tablespaces \
      --databases "$D" 2>"$W/err" | sed -E -e '/enable the sandbox mode/d' -e 's/DEFINER=`[^`]+`@`[^`]+`//g' > "$W/$D.sql"
    if [ ! -s "$W/$D.sql" ] || grep -qi error "$W/err"; then cat "$W/err"; log "STOPPED: dump $A"; unset MYSQL_PWD; return 1; fi
    for T in $(mysql -h "$H" -u "$U" --ssl -N -e "select table_name from information_schema.tables where table_schema='$D' and table_type='BASE TABLE' order by 1"); do
      printf '%s\t%s\n' "$T" "$(mysql -h "$H" -u "$U" --ssl -N -e "select count(*) from \`$D\`.\`$T\`")"
    done > "$W/counts.tsv"
    unset MYSQL_PWD
    log "$A: $(wc -l < "$W/counts.tsv") tables, $(awk '{s+=$2} END {print s}' "$W/counts.tsv") rows"
    tar -czf "$W/$A.tar.gz" -C "$W" "$D.sql" counts.tsv
    aws s3 cp "$W/$A.tar.gz" "s3://$BUCKET/$TS/$A.tar.gz" --only-show-errors
    aws s3 presign "s3://$BUCKET/$TS/$A.tar.gz" --expires-in 7200 > "$STATE/$A.url"
    rm -rf "$W"
    log "$A: deleting temporary copy $I"
    aws rds delete-db-instance --db-instance-identifier "$I" --skip-final-snapshot --delete-automated-backups >/dev/null
  done
  echo
  echo "=== Copy these two links into Azure Cloud Shell when asked (valid 2 hours; don't paste them into chat) ==="
  echo "CRM link:"; cat "$STATE/crm.url"; echo
  echo "Platform link:"; cat "$STATE/platform.url"; echo
  for A in crm platform; do aws rds wait db-instance-deleted --db-instance-identifier "demeisen-azure-export-$A"; done
  aws ec2 delete-security-group --group-id "$SG" && log "Temporary copies and their firewall rule removed."
}

case "${1:-}" in start) start ;; finish) finish ;; *) echo "Usage: bash $0 start|finish" ;; esac
