#!/usr/bin/env bash
# Demeisen v2: copy AWS v2 into Azure (used for the cut-over on 2026-11-20). Azure Cloud Shell, run in the background:
#   nohup bash cutover-azure.sh > ~/cutover.log 2>&1 &   (expects the bundle link in ~/.cutover-url; see ops/AZURE.md)
# AWS side: export-v2-for-azure.sh start|finish, then bundle databases + files + audit (ops/AZURE.md).
# 1) platform app: write-once audit container + settings  2) loads both databases from the AWS v2 export and checks
# every table's row count  3) copies stored files and audit records  4) removes temporary access. Re-runnable.
set -uo pipefail
az account set --subscription d8a79507-6e06-4265-9d9a-f7f05b4a9b79 || { echo "STOPPED: subscription"; exit 1; }
RG=Non_Inv; KV=demeisen-kv-c9895a; SA=demeisenv2wdjkbofw; FD=https://demeisen-v2-ddacbfayfrb7g9ge.z03.azurefd.net
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
# The MySQL servers carry a CanNotDelete lock ("do-not-delete"), which also blocks deleting firewall rules. Closing the
# temporary rule therefore lifts the lock for a moment and always puts it back.
fw_open() { az mysql flexible-server firewall-rule create -g $RG -n "$1" --rule-name cutover --start-ip-address "$2" --end-ip-address "$2" -o none; }
fw_close() {
  az lock delete -g $RG -n do-not-delete --resource "$1" --resource-type Microsoft.DBforMySQL/flexibleServers -o none
  az mysql flexible-server firewall-rule delete -g $RG -n "$1" --rule-name cutover --yes -o none; local rc=$?
  az lock create -g $RG -n do-not-delete --lock-type CanNotDelete --resource "$1" --resource-type Microsoft.DBforMySQL/flexibleServers \
    --notes "Demeisen v2 database: protect from deletion" -o none || log "WARNING: could not restore the lock on $1 - restore it now"
  [ $rc = 0 ] && log "$1: firewall rule removed, lock restored" || log "WARNING: firewall rule 'cutover' still on $1 - remove it"
}
W=~/cutover; rm -rf "$W"; mkdir -p "$W"; cd "$W" || exit 1
log "Downloading the AWS v2 export"
curl -fsS -o bundle.tar "$(cat ~/.cutover-url)" || { echo "STOPPED: download failed (link expired or cut short)"; exit 1; }
rm -f ~/.cutover-url
tar -xf bundle.tar || { echo "STOPPED: unpack"; exit 1; }
log "Export contains: $(ls | tr '\n' ' ')"

log "Updating the platform app (write-once audit container)"
curl -fsS -o platform-app.bicep https://raw.githubusercontent.com/HitmanH/demeisen_intranet_v2_platform/main/infra/azure/app.bicep \
  && grep -q audit-worm platform-app.bicep || { echo "STOPPED: GitHub doesn't have the cut-over template yet; wait 5 minutes and re-run"; exit 1; }
OV=$(az keyvault secret show --vault-name $KV -n origin-verify --query value -o tsv)
az deployment group create -g $RG -n demeisen-v2-platform-app -f platform-app.bicep -p keyVaultName=$KV \
  enableWeb=true publicUrl=$FD originVerify="$OV" -o none || { echo "STOPPED: platform app - see error above"; exit 1; }
unset OV
log "Platform app updated"

IP=$(curl -s https://checkip.amazonaws.com)
if mysql --version | grep -qi mariadb; then SSL=--ssl; else SSL=--ssl-mode=REQUIRED; fi
ALLOK=1
for S in crm platform; do
  D=$([ $S = crm ] && echo crm || echo intranet); SRV=demeisen-$S-mysql; H=$SRV.mysql.database.azure.com
  mkdir -p $S && tar -xzf $S.tar.gz -C $S || { echo "STOPPED: unpack $S"; exit 1; }
  sed -i -e '/GTID_PURGED/,/;/d' -e '/SQL_LOG_BIN/d' $S/$D.sql
  fw_open $SRV "$IP" || { echo "STOPPED: firewall $S"; exit 1; }
  export MYSQL_PWD=$(az keyvault secret show --vault-name $KV -n mysql-$S-admin --query value -o tsv)
  log "$S: saving the current Azure copy first (anything entered on Azure since yesterday)"
  mysqldump -h $H -u demeisenadmin $SSL --single-transaction --routines --triggers --no-tablespaces --set-gtid-purged=OFF \
    --databases $D > $S/azure-before-cutover.sql 2>$S/dump.err || mysqldump -h $H -u demeisenadmin $SSL --single-transaction \
    --routines --triggers --no-tablespaces --databases $D > $S/azure-before-cutover.sql 2>$S/dump.err
  [ -s $S/azure-before-cutover.sql ] || { cat $S/dump.err; fw_close $SRV; echo "STOPPED: could not save the Azure copy of $D - nothing loaded"; exit 1; }
  mkdir -p before && cp $S/azure-before-cutover.sql before/$D-azure-before-cutover-$(date -u +%Y%m%d-%H%M).sql
  for T in $(cut -f1 $S/counts.tsv); do
    printf '%s\t%s\n' "$T" "$(mysql -h $H -u demeisenadmin $SSL -N -e "select count(*) from \`$D\`.\`$T\`" 2>/dev/null)"
  done > $S/azure-before.tsv
  log "$S: rows on Azure before: $(awk '{s+=$2} END {print s}' $S/azure-before.tsv), in the AWS export: $(awk '{s+=$2} END {print s}' $S/counts.tsv)"
  diff -q $S/counts.tsv $S/azure-before.tsv >/dev/null || { log "$S: tables that differ (AWS left, Azure-before right):"; diff -y --suppress-common-lines $S/counts.tsv $S/azure-before.tsv; }
  log "$S: loading $D (replaces the test data on Azure)"
  if mysql -h $H -u demeisenadmin $SSL < $S/$D.sql; then
    for T in $(cut -f1 $S/counts.tsv); do
      printf '%s\t%s\n' "$T" "$(mysql -h $H -u demeisenadmin $SSL -N -e "select count(*) from \`$D\`.\`$T\`")"
    done > $S/azure.tsv
    if diff $S/counts.tsv $S/azure.tsv >/dev/null; then
      log "$S: OK - $(wc -l < $S/azure.tsv) tables, $(awk '{s+=$2} END {print s}' $S/azure.tsv) rows, all match AWS v2"
    else ALLOK=0; log "$S: MISMATCH (AWS left, Azure right):"; diff -y $S/counts.tsv $S/azure.tsv; fi
  else ALLOK=0; log "$S: LOAD FAILED - see error above"; fi
  unset MYSQL_PWD
  fw_close $SRV
done

log "Copying stored files and audit records"
ME=$(az ad signed-in-user show --query id -o tsv); SAID=$(az storage account show -g $RG -n $SA --query id -o tsv)
az role assignment create --assignee-object-id "$ME" --assignee-principal-type User --role "Storage Blob Data Contributor" --scope "$SAID" -o none
up() {  # $1 local dir, $2 container, $3 overwrite true/false
  [ -d "$1" ] && [ -n "$(ls -A "$1")" ] || { log "$2: nothing to copy"; return 0; }
  for _ in $(seq 1 30); do
    az storage blob upload-batch --account-name $SA --auth-mode login -d "$2" -s "$1" --overwrite "$3" --only-show-errors -o none 2>/dev/null && {
      log "$2: $(find "$1" -type f | wc -l) files copied"; return 0; }
    sleep 10
  done
  ALLOK=0; log "$2: COPY FAILED"
}
mkdir -p files/cutover-backup && cp before/* files/cutover-backup/ 2>/dev/null
up files files true
up audit audit-worm false
az role assignment delete --assignee "$ME" --role "Storage Blob Data Contributor" --scope "$SAID" -o none && log "Temporary upload rights removed"
cd ~ && rm -rf "$W"
c() { curl -s -m 20 -o /dev/null -w '%{http_code}' "$1"; }
log "Checks: intranet $(c $FD/healthz) (200), CRM page $(c $FD/crm/) (200), CRM data $(c $FD/crm/api/data) (401)"
[ $ALLOK = 1 ] && echo "CUT-OVER DONE: everything matches. Azure is now the live system: $FD" || echo "CUT-OVER FINISHED WITH PROBLEMS - paste this log"
