#!/usr/bin/env bash
# Demeisen v2: move the intranet to its own names on demeisen.io (DNS at Porkbun). Azure Cloud Shell:
#   bash custom-domains.sh attach     1) attach mycroft.demeisen.io + crm.demeisen.io to the front door (sign-in unchanged)
#   bash custom-domains.sh signin     3) after the AWS step (ops/AZURE.md): the platform signs people in at mycroft
#   bash custom-domains.sh retire-old 4) send the old azurefd.net address to mycroft too
#   bash custom-domains.sh rollback   sign-in back on the azurefd.net address (then set the SSM parameter back too)
# Each step re-runs the repo's templates from GitHub main, so merge the change first. Re-runnable.
set -euo pipefail
az account set --subscription d8a79507-6e06-4265-9d9a-f7f05b4a9b79
RG=Non_Inv; KV=demeisen-kv-c9895a; FD=demeisen-v2-fd
MAIN=mycroft.demeisen.io; CRM=crm.demeisen.io; OLD=https://demeisen-v2-ddacbfayfrb7g9ge.z03.azurefd.net
RAW=https://raw.githubusercontent.com/HitmanH/demeisen_intranet_v2_platform/main/infra/azure
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

frontdoor() {  # $1 = redirectDefaultDomain (true|false)
  curl -fsS -o frontdoor.bicep "$RAW/frontdoor.bicep" && grep -q mainDomain frontdoor.bicep \
    || { echo "STOPPED: GitHub main doesn't have the custom-domain template yet: merge it, wait 5 minutes, re-run"; exit 1; }
  log "Custom domains on the front door (the template adopts these names):"
  az afd custom-domain list -g $RG --profile-name $FD --query "[].{name:name, host:hostName, validation:domainValidationState}" -o table
  for d in $MAIN $CRM; do
    n=$(az afd custom-domain list -g $RG --profile-name $FD --query "[?hostName=='$d'].name" -o tsv)
    [ "$n" = "${d//./-}" ] || { echo "STOPPED: $d is named '$n' in Azure, expected '${d//./-}'"; exit 1; }
  done
  CRMHOST=$(az afd origin show -g $RG --profile-name $FD --origin-group-name crm --origin-name crm --query hostName -o tsv)
  PLATHOST=$(az afd origin show -g $RG --profile-name $FD --origin-group-name platform --origin-name platform --query hostName -o tsv)
  OV=$(az keyvault secret show --vault-name $KV -n origin-verify --query value -o tsv)
  az deployment group create -g $RG -n demeisen-v2-frontdoor -f frontdoor.bicep -p crmHost="$CRMHOST" platformHost="$PLATHOST" \
    originVerify="$OV" mainDomain=$MAIN crmDomain=$CRM redirectDefaultDomain=$1 --query properties.outputs -o json
  unset OV
  log "Front door updated. Changes take up to 15 minutes to reach every edge; certificates can take up to an hour."
}

platform_url() {  # $1 = the address the platform signs people in at
  curl -fsS -o platform-app.bicep "$RAW/app.bicep"
  OV=$(az keyvault secret show --vault-name $KV -n origin-verify --query value -o tsv)
  az deployment group create -g $RG -n demeisen-v2-platform-app -f platform-app.bicep -p keyVaultName=$KV \
    enableWeb=true publicUrl="$1" originVerify="$OV" -o none
  unset OV
  log "Platform signs people in at $1"
}

W=~/custom-domains; mkdir -p "$W"; cd "$W"
case "${1:-}" in
  attach)     frontdoor false
              log "Check: https://$MAIN/healthz should answer OK, https://$CRM should go to https://$MAIN/crm/" ;;
  signin)     platform_url "https://$MAIN" ;;
  retire-old) frontdoor true ;;
  rollback)   frontdoor false; platform_url "$OLD" ;;
  *) echo "usage: bash custom-domains.sh attach|signin|retire-old|rollback"; exit 2 ;;
esac
