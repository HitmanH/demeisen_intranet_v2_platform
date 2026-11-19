// Demeisen v2 on Azure - step 7: the front door. One HTTPS address for everything:
//   /crm, /crm/*  -> the CRM app          everything else (/, /auth/*, ...) -> the platform app
// Every request it forwards carries X-Origin-Verify (a secret from Key Vault); the apps refuse requests without it,
// so the apps' own addresses can't be used to get round the front door. No caching: every page is personal.
// Run without origins first to get the address; run again with the apps' hostnames to connect them.
//   az deployment group create -g Non_Inv -f frontdoor.bicep [-p crmHost=... platformHost=... originVerify=...]
targetScope = 'resourceGroup'

param crmHost string = ''
param platformHost string = ''
@secure()
param originVerify string = ''

var tags = { system: 'demeisen-v2', managedBy: 'platform/infra/azure/frontdoor.bicep' }
var connect = !empty(crmHost) && !empty(platformHost) && !empty(originVerify)

resource profile 'Microsoft.Cdn/profiles@2024-02-01' = {
  name: 'demeisen-v2-fd'
  location: 'global'
  tags: tags
  sku: { name: 'Standard_AzureFrontDoor' }
  properties: { originResponseTimeoutSeconds: 120 }
}

resource endpoint 'Microsoft.Cdn/profiles/afdEndpoints@2024-02-01' = {
  parent: profile
  name: 'demeisen-v2'
  location: 'global'
  tags: tags
  properties: { enabledState: 'Enabled' }
}

var apps = [
  { key: 'crm', host: crmHost, patterns: ['/crm', '/crm/*'] }
  { key: 'platform', host: platformHost, patterns: ['/*'] }
]

resource groups 'Microsoft.Cdn/profiles/originGroups@2024-02-01' = [for a in apps: if (connect) {
  parent: profile
  name: a.key
  properties: {
    loadBalancingSettings: { sampleSize: 4, successfulSamplesRequired: 3, additionalLatencyInMilliseconds: 50 }
    healthProbeSettings: { probePath: '/healthz', probeRequestType: 'GET', probeProtocol: 'Https', probeIntervalInSeconds: 100 }
    sessionAffinityState: 'Disabled'
  }
}]

resource origins 'Microsoft.Cdn/profiles/originGroups/origins@2024-02-01' = [for (a, i) in apps: if (connect) {
  parent: groups[i]
  name: a.key
  properties: {
    hostName: a.host
    originHostHeader: a.host
    httpPort: 80
    httpsPort: 443
    priority: 1
    weight: 1000
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
  }
}]

resource ruleSet 'Microsoft.Cdn/profiles/ruleSets@2024-02-01' = if (connect) {
  parent: profile
  name: 'originverify'
}
resource verifyRule 'Microsoft.Cdn/profiles/ruleSets/rules@2024-02-01' = if (connect) {
  parent: ruleSet
  name: 'addheader'
  properties: {
    order: 1
    conditions: []
    actions: [{
      name: 'ModifyRequestHeader'
      parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Overwrite', headerName: 'X-Origin-Verify', value: originVerify }
    }]
    matchProcessingBehavior: 'Continue'
  }
}

@batchSize(1)
resource routes 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-02-01' = [for (a, i) in apps: if (connect) {
  parent: endpoint
  name: a.key
  dependsOn: [origins, verifyRule]
  properties: {
    originGroup: { id: groups[i].id }
    ruleSets: [{ id: ruleSet.id }]
    supportedProtocols: ['Http', 'Https']
    patternsToMatch: a.patterns
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Enabled'
    enabledState: 'Enabled'
  }
}]

output url string = 'https://${endpoint.properties.hostName}'
