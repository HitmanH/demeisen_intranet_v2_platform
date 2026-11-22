// Demeisen v2 on Azure - step 7: the front door. One HTTPS address for everything:
//   /crm, /crm/*  -> the CRM app          everything else (/, /auth/*, ...) -> the platform app
// Every request it forwards carries X-Origin-Verify (a secret from Key Vault); the apps refuse requests without it,
// so the apps' own addresses can't be used to get round the front door. No caching: every page is personal.
// Run without origins first to get the address; run again with the apps' hostnames to connect them.
//   az deployment group create -g Non_Inv -f frontdoor.bicep [-p crmHost=... platformHost=... originVerify=...]
// Custom names (DNS at Porkbun, CNAME -> the endpoint; certificates managed by Front Door): mycroft.demeisen.io is the
// address people use; crm.demeisen.io sends people to mycroft.demeisen.io/crm/ (one sign-in, one cookie, one origin
// per app). redirectDefaultDomain=true also sends the old azurefd.net address there. ops/azure/custom-domains.sh runs it.
targetScope = 'resourceGroup'

param crmHost string = ''
param platformHost string = ''
@secure()
param originVerify string = ''
@description('The address people use, e.g. mycroft.demeisen.io (empty = only the azurefd.net address)')
param mainDomain string = ''
@description('A name that only redirects to mainDomain + /crm/, e.g. crm.demeisen.io (empty = none)')
param crmDomain string = ''
@description('Also redirect the azurefd.net address to mainDomain (after sign-in has moved there)')
param redirectDefaultDomain bool = false

var tags = { system: 'demeisen-v2', managedBy: 'platform/infra/azure/frontdoor.bicep' }
var connect = !empty(crmHost) && !empty(platformHost) && !empty(originVerify)
// Resource names as the portal made them (dots -> hyphens), so this template adopts the existing domains.
var domainNames = filter([mainDomain, crmDomain], d => !empty(d))

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

resource domains 'Microsoft.Cdn/profiles/customDomains@2024-02-01' = [for d in domainNames: {
  parent: profile
  name: replace(d, '.', '-')
  properties: {
    hostName: d
    tlsSettings: { certificateType: 'ManagedCertificate', minimumTlsVersion: 'TLS12' }
  }
}]

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

// Redirects, before anything else: crm.demeisen.io -> mainDomain/crm/; optionally azurefd.net (host '') -> mainDomain.
var redirects = concat(
  empty(crmDomain) || empty(mainDomain) ? [] : [{ name: 'crmtomain', host: crmDomain, path: '/crm/' }],
  redirectDefaultDomain && !empty(mainDomain) ? [{ name: 'defaulttomain', host: '', path: '' }] : [])
resource redirectSet 'Microsoft.Cdn/profiles/ruleSets@2024-02-01' = if (connect && !empty(redirects)) {
  parent: profile
  name: 'hostredirect'
}
@batchSize(1)
resource redirectRules 'Microsoft.Cdn/profiles/ruleSets/rules@2024-02-01' = [for (r, i) in redirects: if (connect) {
  parent: redirectSet
  name: r.name
  properties: {
    order: i + 1
    conditions: [{
      name: 'HostName'
      parameters: { typeName: 'DeliveryRuleHostNameConditionParameters', operator: 'Equal', negateCondition: false, matchValues: [empty(r.host) ? endpoint.properties.hostName : r.host], transforms: ['Lowercase'] }
    }]
    actions: [{
      name: 'UrlRedirect'
      parameters: union({ typeName: 'DeliveryRuleUrlRedirectActionParameters', redirectType: 'Found', destinationProtocol: 'Https', customHostname: mainDomain },
        empty(r.path) ? {} : { customPath: r.path })
    }]
    matchProcessingBehavior: 'Stop'
  }
}]

@batchSize(1)
resource routes 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-02-01' = [for (a, i) in apps: if (connect) {
  parent: endpoint
  name: a.key
  dependsOn: [origins, verifyRule, redirectRules]
  properties: {
    originGroup: { id: groups[i].id }
    ruleSets: concat(empty(redirects) ? [] : [{ id: redirectSet.id }], [{ id: ruleSet.id }])
    customDomains: [for (d, j) in domainNames: { id: domains[j].id }]
    supportedProtocols: ['Http', 'Https']
    patternsToMatch: a.patterns
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Enabled'
    enabledState: 'Enabled'
  }
}]

output url string = empty(mainDomain) ? 'https://${endpoint.properties.hostName}' : 'https://${mainDomain}'
output defaultUrl string = 'https://${endpoint.properties.hostName}'
