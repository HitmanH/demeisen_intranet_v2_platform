// Demeisen v2 on Azure - step 1: foundations. Holds no data and changes no data.
// Creates the private network, private links to the two existing MySQL servers, logging, the container registry,
// the Container Apps environment, a managed identity per app, and the files/audit storage.
// Re-runnable: deploying again only brings things back in line with this file.
//   az deployment group create -g Non_Inv -f foundations.bicep -p keyVaultName=<demeisen-kv-xxxxxx>
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param keyVaultName string
param crmMysqlName string = 'demeisen-crm-mysql'
param platformMysqlName string = 'demeisen-platform-mysql'

var sfx = take(uniqueString(resourceGroup().id), 8)
var tags = { system: 'demeisen-v2', managedBy: 'foundations.bicep' }

// ---------- Network: apps subnet (Container Apps) + private-endpoint subnet ----------
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'demeisen-v2-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.60.0.0/16'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.60.0.0/23'
          delegations: [{ name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'private-endpoints'
        properties: { addressPrefix: '10.60.2.0/24' }
      }
    ]
  }
}

// ---------- Private links to the existing MySQL servers (servers themselves are not changed) ----------
resource mysqlDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.mysql.database.azure.com'
  location: 'global'
  tags: tags
}
resource mysqlDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: mysqlDns
  name: 'demeisen-v2-vnet'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: vnet.id } }
}
resource crmMysql 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' existing = { name: crmMysqlName }
resource platformMysql 'Microsoft.DBforMySQL/flexibleServers@2023-12-30' existing = { name: platformMysqlName }

var mysqlServers = [
  { key: 'crm', id: crmMysql.id }
  { key: 'platform', id: platformMysql.id }
]
resource mysqlPe 'Microsoft.Network/privateEndpoints@2024-05-01' = [for s in mysqlServers: {
  name: 'demeisen-${s.key}-mysql-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: vnet.properties.subnets[1].id }
    privateLinkServiceConnections: [{ name: 'mysql', properties: { privateLinkServiceId: s.id, groupIds: ['mysqlServer'] } }]
  }
}]
resource mysqlPeDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (s, i) in mysqlServers: {
  parent: mysqlPe[i]
  name: 'default'
  properties: { privateDnsZoneConfigs: [{ name: 'mysql', properties: { privateDnsZoneId: mysqlDns.id } }] }
}]

// ---------- Logging ----------
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'demeisen-v2-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 90 }
}

// ---------- Container registry (no admin user; apps pull with their identity) ----------
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'demeisenv2${sfx}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ---------- One identity per app ----------
var apps = ['platform', 'crm']
resource ids 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for a in apps: {
  name: 'demeisen-v2-${a}'
  location: location
  tags: tags
}]
var acrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
resource acrPullAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (a, i) in apps: {
  name: guid(acr.id, ids[i].id, acrPull)
  scope: acr
  properties: { roleDefinitionId: acrPull, principalId: ids[i].properties.principalId, principalType: 'ServicePrincipal' }
}]

// ---------- Files and audit storage (replaces the S3 files and audit buckets) ----------
resource store 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'demeisenv2${sfx}'
  location: location
  tags: tags
  sku: { name: 'Standard_ZRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}
resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: store
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
  }
}
resource filesC 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'files'
}
resource auditC 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobSvc
  name: 'audit'
}
var blobContrib = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
resource platformFiles 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(filesC.id, ids[0].id, blobContrib)
  scope: filesC
  properties: { roleDefinitionId: blobContrib, principalId: ids[0].properties.principalId, principalType: 'ServicePrincipal' }
}
resource platformAudit 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(auditC.id, ids[0].id, blobContrib)
  scope: auditC
  properties: { roleDefinitionId: blobContrib, principalId: ids[0].properties.principalId, principalType: 'ServicePrincipal' }
}

// ---------- Key Vault (existing, access-policy model): apps may read secrets only ----------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: keyVaultName }
resource kvRead 'Microsoft.KeyVault/vaults/accessPolicies@2023-07-01' = {
  parent: kv
  name: 'add'
  properties: {
    accessPolicies: [for (a, i) in apps: {
      tenantId: subscription().tenantId
      objectId: ids[i].properties.principalId
      permissions: { secrets: ['get'] }
    }]
  }
}

// ---------- Container Apps environment, inside the network ----------
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'demeisen-v2-env'
  location: location
  tags: tags
  properties: {
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    vnetConfiguration: { infrastructureSubnetId: vnet.properties.subnets[0].id, internal: false }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey }
    }
  }
}

output registry string = acr.properties.loginServer
output storageAccount string = store.name
output environment string = env.name
output environmentDomain string = env.properties.defaultDomain
output platformIdentityClientId string = ids[0].properties.clientId
output crmIdentityClientId string = ids[1].properties.clientId
