// Demeisen v2 on Azure - step 2: passwordless deploys from GitHub (no stored secrets).
// One deploy identity per repo. GitHub Actions in that repo may sign in as it ONLY from the main branch or from a job in
// the GitHub environment "data" (which needs approval). Mirrors the AWS bootstrap roles. GitHub sends the subject in
// either the name form (repo:HitmanH/<repo>:...) or the ID form (repo:HitmanH@<ownerId>/<repo>@<repoId>:...);
// Azure needs exact matches, so both are trusted.
// Rights now: push images to the registry. Rights on the apps themselves are added when the apps are created (step 6).
//   az deployment group create -g Non_Inv -f deploy-identities.bicep -p crmRepoId=<id> platformRepoId=<id>
targetScope = 'resourceGroup'

param location string = resourceGroup().location
param owner string = 'HitmanH'
param ownerId string = '5677860'
param crmRepoId string
param platformRepoId string

var tags = { system: 'demeisen-v2', managedBy: 'deploy-identities.bicep' }
var repos = [
  { key: 'crm', name: 'demeisen_intranet_v2_crm', id: crmRepoId }
  { key: 'platform', name: 'demeisen_intranet_v2_platform', id: platformRepoId }
]
var acrPush = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'demeisenv2${take(uniqueString(resourceGroup().id), 8)}'
}

resource deployIds 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for r in repos: {
  name: 'demeisen-v2-deploy-${r.key}'
  location: location
  tags: tags
}]

module creds 'deploy-identity-creds.bicep' = [for (r, i) in repos: {
  name: 'creds-${r.key}'
  params: {
    identityName: deployIds[i].name
    subjects: [
      'repo:${owner}/${r.name}:ref:refs/heads/main'
      'repo:${owner}@${ownerId}/${r.name}@${r.id}:ref:refs/heads/main'
      'repo:${owner}/${r.name}:environment:data'
      'repo:${owner}@${ownerId}/${r.name}@${r.id}:environment:data'
    ]
  }
}]

resource push 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (r, i) in repos: {
  name: guid(acr.id, deployIds[i].id, acrPush)
  scope: acr
  properties: { roleDefinitionId: acrPush, principalId: deployIds[i].properties.principalId, principalType: 'ServicePrincipal' }
}]

output crmDeployClientId string = deployIds[0].properties.clientId
output platformDeployClientId string = deployIds[1].properties.clientId
output tenantId string = subscription().tenantId
output subscriptionId string = subscription().subscriptionId
