#!/usr/bin/env bash
# Demeisen v2: turn on Demeisen's own sign-in (password + authenticator code), trial mode. Azure Cloud Shell:
#   bash own-signin.sh trial     creates the two sign-in keys in Key Vault (never shown), switches it on in the intranet
#                                and lets the CRM sign in through it on crm.demeisen.io. Everyone else is unchanged:
#                                mycroft.demeisen.io still sends people to the previous (Cognito) sign-in, and
#                                /signin (own sign-in) also offers "Use the previous sign-in".
#   bash own-signin.sh everyone  after the trial: people who aren't signed in go to the own sign-in, on every address
#   bash own-signin.sh off       back to Cognito only (own accounts are kept, just not used)
# Needs GitHub main to have the own-sign-in change merged (and deployed) first. Re-runnable.
set -euo pipefail
az account set --subscription d8a79507-6e06-4265-9d9a-f7f05b4a9b79
RG=Non_Inv; KV=demeisen-kv-c9895a; HUB=https://mycroft.demeisen.io
RAW=https://raw.githubusercontent.com/HitmanH/demeisen_intranet_v2
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
W=~/own-signin; mkdir -p "$W"; cd "$W"
curl -fsS -o platform-app.bicep "$RAW"_platform/main/infra/azure/app.bicep
curl -fsS -o crm-app.bicep "$RAW"_crm/main/infra/azure/app.bicep
grep -q ownSignin platform-app.bicep && grep -q hubHosts crm-app.bicep \
  || { echo "STOPPED: GitHub main doesn't have the own-sign-in change yet: merge both, wait for the deploys, re-run"; exit 1; }

keys() {
  if ! az keyvault secret show --vault-name $KV -n platform-accounts-key -o none 2>/dev/null; then
    az keyvault secret set --vault-name $KV -n platform-accounts-key --value "$(openssl rand -hex 32)" -o none
    log "Created platform-accounts-key (encrypts the 2FA secrets)"
  fi
  if ! az keyvault secret show --vault-name $KV -n platform-oidc-signing-key -o none 2>/dev/null; then
    umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out k.pem 2>/dev/null
    az keyvault secret set --vault-name $KV -n platform-oidc-signing-key --file k.pem -o none
    shred -u k.pem 2>/dev/null || rm -f k.pem
    log "Created platform-oidc-signing-key (signs the CRM's sign-in tokens)"
  fi
}

apply() {  # $1 ownSignin true|false   $2 signinDefault cognito|local   $3 CRM hubHosts
  # Keep the live sign-in address exactly as it is today.
  PUB=$(az containerapp show -g $RG -n demeisen-v2-platform --query "properties.template.containers[0].env[?name=='PUBLIC_URL'].value | [0]" -o tsv)
  [ -n "$PUB" ] || { echo "STOPPED: couldn't read the platform's current PUBLIC_URL"; exit 1; }
  log "Platform signs people in at $PUB (unchanged)"
  OV=$(az keyvault secret show --vault-name $KV -n origin-verify --query value -o tsv)
  az deployment group create -g $RG -n demeisen-v2-platform-app -f platform-app.bicep -p keyVaultName=$KV \
    enableWeb=true publicUrl="$PUB" originVerify="$OV" ownSignin=$1 signinDefault=$2 signinIssuer=$HUB -o none
  log "Intranet updated (own sign-in: $1, default: $2)"
  az deployment group create -g $RG -n demeisen-v2-crm-app -f crm-app.bicep -p keyVaultName=$KV originVerify="$OV" \
    hubIssuer="$([ "$1" = true ] && echo $HUB)" hubHosts="$3" -o none
  unset OV
  log "CRM updated (own sign-in on: ${3:-none})"
}

check() {
  sleep 30
  log "Checks (expect 200 each):"
  for u in "$HUB/healthz" "$HUB/signin" "$HUB/.well-known/openid-configuration" "$HUB/oauth2/jwks" "https://crm.demeisen.io/crm/"; do
    echo "  $(curl -s -o /dev/null -w '%{http_code}' "$u")  $u"
  done
}

case "${1:-}" in
  trial)    keys; apply true cognito crm.demeisen.io; check
            log "Next: sign in at $HUB as usual, open Settings > Users, press 'Setup link' on your own row and open it." ;;
  everyone) keys; apply true local '*'; check ;;
  off)      apply false cognito ''; log "Own sign-in is off; Cognito only." ;;
  *) echo "usage: bash own-signin.sh trial|everyone|off"; exit 2 ;;
esac
