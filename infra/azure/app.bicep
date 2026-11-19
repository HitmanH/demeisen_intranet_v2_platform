// Demeisen platform on Azure (step 6): the intranet web app (portal, MNDA, memos, documents) and its admin job.
// Holds no data: the database is the existing demeisen-platform-mysql server (private link); files and the audit copy
// are in the storage account from foundations.bicep.
// The web app is created only when enableWeb=true, which needs sign-in set up first (Cognito client for this address,
// secrets platform-oidc-client-secret and platform-flask-secret in Key Vault): the app refuses to start without them.
// Run by an Owner from Cloud Shell after GitHub has built the first image (deploy-azure.yml); afterwards GitHub only
// swaps the image on each merge. Re-running is safe.
//   az deployment group create -g Non_Inv -f app.bicep -p keyVaultName=<demeisen-kv-xxxxxx> [enableWeb=true ...]
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param keyVaultName string
param imageTag string = 'latest'
param enableWeb bool = false
// Sign-in (used only when enableWeb): the Demeisen Cognito pool, with an app client for the Azure address.
param oidcIssuer string = 'https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_o84PDILyl'
param oidcClientId string = ''
param oidcLogoutUrl string = 'https://demeisen-crm-poc.auth.eu-west-2.amazoncognito.com/logout'
@description('The address people use (the front door, or the app\'s own address before step 7), e.g. https://xyz.azurefd.net')
param publicUrl string = ''
@secure()
param originVerify string = ''

var sfx = take(uniqueString(resourceGroup().id), 8)
var tags = { system: 'demeisen-v2', app: 'platform', managedBy: 'platform/infra/azure/app.bicep' }

resource env 'Microsoft.App/managedEnvironments@2024-03-01' existing = { name: 'demeisen-v2-env' }
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: 'demeisenv2${sfx}' }
resource store 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: 'demeisenv2${sfx}' }
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: keyVaultName }
resource appId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: 'demeisen-v2-platform' }
resource deployId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: 'demeisen-v2-deploy-platform' }

var image = '${acr.properties.loginServer}/demeisen-platform:${imageTag}'
var kvUri = kv.properties.vaultUri
var registries = [{ server: acr.properties.loginServer, identity: appId.id }]
var commonEnv = [
  { name: 'DB_HOST', value: 'demeisen-platform-mysql.mysql.database.azure.com' }
  { name: 'DB_USER', value: 'platform_app' }
  { name: 'DB_NAME', value: 'intranet' }
  { name: 'DB_PASSWORD', secretRef: 'db-password' }
  { name: 'AZURE_CLIENT_ID', value: appId.properties.clientId }
  { name: 'AZURE_STORAGE_ACCOUNT', value: store.name }
  { name: 'CRM_URL', value: '/crm/' }
]

// ---------- Web app (only once sign-in is ready) ----------
resource web 'Microsoft.App/containerApps@2024-03-01' = if (enableWeb) {
  name: 'demeisen-v2-platform'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${appId.id}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8080, transport: 'auto', allowInsecure: false }
      registries: registries
      secrets: concat([
        { name: 'db-password', keyVaultUrl: '${kvUri}secrets/mysql-platform-app', identity: appId.id }
        { name: 'flask-secret', keyVaultUrl: '${kvUri}secrets/platform-flask-secret', identity: appId.id }
        { name: 'oidc-client-secret', keyVaultUrl: '${kvUri}secrets/platform-oidc-client-secret', identity: appId.id }
      ], empty(originVerify) ? [] : [{ name: 'origin-verify', value: originVerify }])
    }
    template: {
      containers: [{
        name: 'platform'
        image: image
        resources: { cpu: json('1.0'), memory: '2Gi' }   // LibreOffice (NDA/memo PDFs) needs the room
        env: concat(commonEnv, [
          { name: 'INTRANET_SECRET', secretRef: 'flask-secret' }
          { name: 'OIDC_ISSUER', value: oidcIssuer }
          { name: 'OIDC_CLIENT_ID', value: oidcClientId }
          { name: 'OIDC_CLIENT_SECRET', secretRef: 'oidc-client-secret' }
          { name: 'OIDC_LOGOUT_URL', value: oidcLogoutUrl }
          { name: 'PUBLIC_URL', value: publicUrl }
        ], empty(originVerify) ? [] : [{ name: 'ORIGIN_VERIFY', secretRef: 'origin-verify' }])
        probes: [{ type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30, initialDelaySeconds: 10 }]
      }]
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
}

// ---------- Admin job: schema migrations and grants (run by each GitHub deploy) ----------
resource admin 'Microsoft.App/jobs@2024-03-01' = {
  name: 'demeisen-v2-platform-admin'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${appId.id}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 600
      replicaRetryLimit: 0
      registries: registries
      secrets: [
        { name: 'db-password', keyVaultUrl: '${kvUri}secrets/mysql-platform-app', identity: appId.id }
        { name: 'db-migrator-password', keyVaultUrl: '${kvUri}secrets/mysql-platform-migrator', identity: appId.id }
      ]
    }
    template: {
      containers: [{
        name: 'admin'
        image: image
        command: ['python', '-m', 'portal.admin', 'migrate']
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(commonEnv, [{ name: 'DB_MIGRATOR_PASSWORD', secretRef: 'db-migrator-password' }])
      }]
    }
  }
}

// ---------- GitHub deploy identity: may update only these, and use the environment ----------
var contributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
resource deployWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableWeb) {
  name: guid(resourceGroup().id, 'demeisen-v2-platform', deployId.id, contributor)
  scope: web
  properties: { roleDefinitionId: contributor, principalId: deployId.properties.principalId, principalType: 'ServicePrincipal' }
}
resource deployAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(admin.id, deployId.id, contributor)
  scope: admin
  properties: { roleDefinitionId: contributor, principalId: deployId.properties.principalId, principalType: 'ServicePrincipal' }
}
resource envJoin 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'demeisen-v2-aca-environment-user')
  properties: {
    roleName: 'Demeisen v2 Container Apps environment user (${resourceGroup().name})'
    description: 'Read the Demeisen v2 Container Apps environment and place apps in it. Nothing else.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{ actions: ['Microsoft.App/managedEnvironments/read', 'Microsoft.App/managedEnvironments/join/action'] }]
  }
}
resource deployEnv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(env.id, deployId.id, envJoin.id)
  scope: env
  properties: { roleDefinitionId: envJoin.id, principalId: deployId.properties.principalId, principalType: 'ServicePrincipal' }
}

output url string = enableWeb ? 'https://${web!.properties.configuration.ingress.fqdn}/' : 'web app not created yet (enableWeb=false)'
