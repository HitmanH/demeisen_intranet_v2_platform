// Demeisen v2 on Azure - step 7: the front door. One HTTPS address for everything:
//   /crm, /crm/*  -> the CRM app          everything else (/, /auth/*, ...) -> the platform app
// Every request it forwards carries X-Origin-Verify (a secret from Key Vault); the apps refuse requests without it,
// so the apps' own addresses can't be used to get round the front door. No caching: every page is personal.
// Run without origins first to get the address; run again with the apps' hostnames to connect them.
//   az deployment group create -g Non_Inv -f frontdoor.bicep [-p crmHost=... platformHost=... originVerify=...]
// Custom names (DNS at Porkbun, CNAME -> the endpoint; certificates managed by Front Door): mycroft.demeisen.io is the
// intranet's address (CRM also at /crm/); crm.demeisen.io is the CRM's own address: /crm/* there goes to the CRM, and
// anything else there is sent to crm.demeisen.io/crm/ (the CRM signs in with its own Cognito client, so it works on
// either name). redirectDefaultDomain=true also sends the old azurefd.net address to mycroft. custom-domains.sh runs it.
targetScope = 'resourceGroup'

param crmHost string = ''
param platformHost string = ''
@secure()
param originVerify string = ''
@description('The address people use, e.g. mycroft.demeisen.io (empty = only the azurefd.net address)')
param mainDomain string = ''
@description('The CRM\'s own name, e.g. crm.demeisen.io (empty = none); its root redirects to /crm/ on the same name')
param crmDomain string = ''
@description('Azure resource names of those two domains (the portal adds a suffix, e.g. mycroft-demeisen-io-ae9a); empty = dots -> hyphens')
param mainDomainResource string = ''
param crmDomainResource string = ''
@description('Also redirect the azurefd.net address to mainDomain (after sign-in has moved there)')
param redirectDefaultDomain bool = false

var tags = { system: 'demeisen-v2', managedBy: 'platform/infra/azure/frontdoor.bicep' }
var connect = !empty(crmHost) && !empty(platformHost) && !empty(originVerify)
// Adopt the domains created in the portal under their existing resource names.
var domainList = filter([
  { host: mainDomain, name: empty(mainDomainResource) ? replace(mainDomain, '.', '-') : mainDomainResource }
  { host: crmDomain, name: empty(crmDomainResource) ? replace(crmDomain, '.', '-') : crmDomainResource }
], d => !empty(d.host))

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

resource domains 'Microsoft.Cdn/profiles/customDomains@2024-02-01' = [for d in domainList: {
  parent: profile
  name: d.name
  properties: {
    hostName: d.host
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

// Redirects, before anything else.
// crm.demeisen.io: only requests that miss the CRM route (/crm, /crm/*) reach the platform route, so this rule set sits
// on the platform route alone and sends them to crm.demeisen.io/crm/.
resource crmHostSet 'Microsoft.Cdn/profiles/ruleSets@2024-02-01' = if (connect && !empty(crmDomain)) {
  parent: profile
  name: 'crmhost'
}
resource crmHostRule 'Microsoft.Cdn/profiles/ruleSets/rules@2024-02-01' = if (connect && !empty(crmDomain)) {
  parent: crmHostSet
  name: 'tocrmpage'
  properties: {
    order: 1
    conditions: [{
      name: 'HostName'
      parameters: { typeName: 'DeliveryRuleHostNameConditionParameters', operator: 'Equal', negateCondition: false, matchValues: [crmDomain], transforms: ['Lowercase'] }
    }]
    actions: [{
      name: 'UrlRedirect'
      parameters: { typeName: 'DeliveryRuleUrlRedirectActionParameters', redirectType: 'Found', destinationProtocol: 'Https', customHostname: crmDomain, customPath: '/crm/' }
    }]
    matchProcessingBehavior: 'Stop'
  }
}
// The old azurefd.net address -> mainDomain, same path (both routes), once sign-in has moved.
var retireDefault = connect && redirectDefaultDomain && !empty(mainDomain)
resource redirectSet 'Microsoft.Cdn/profiles/ruleSets@2024-02-01' = if (retireDefault) {
  parent: profile
  name: 'hostredirect'
}
resource defaultRule 'Microsoft.Cdn/profiles/ruleSets/rules@2024-02-01' = if (retireDefault) {
  parent: redirectSet
  name: 'defaulttomain'
  properties: {
    order: 1
    conditions: [{
      name: 'HostName'
      parameters: { typeName: 'DeliveryRuleHostNameConditionParameters', operator: 'Equal', negateCondition: false, matchValues: [endpoint.properties.hostName], transforms: ['Lowercase'] }
    }]
    actions: [{
      name: 'UrlRedirect'
      parameters: { typeName: 'DeliveryRuleUrlRedirectActionParameters', redirectType: 'Found', destinationProtocol: 'Https', customHostname: mainDomain }
    }]
    matchProcessingBehavior: 'Stop'
  }
}

@batchSize(1)
resource routes 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-02-01' = [for (a, i) in apps: if (connect) {
  parent: endpoint
  name: a.key
  dependsOn: [origins, verifyRule, crmHostRule, defaultRule]
  properties: {
    originGroup: { id: groups[i].id }
    ruleSets: concat(retireDefault ? [{ id: redirectSet.id }] : [], a.key == 'platform' && !empty(crmDomain) ? [{ id: crmHostSet.id }] : [], [{ id: ruleSet.id }])
    customDomains: [for (d, j) in domainList: { id: domains[j].id }]
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
