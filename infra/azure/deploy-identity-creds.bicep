// Federated credentials for one deploy identity. Added one at a time (Azure rejects parallel writes to one identity).
param identityName string
param subjects array

resource id 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: identityName }

@batchSize(1)
resource cred 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [for (s, i) in subjects: {
  parent: id
  name: 'github-${i}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: s
    audiences: ['api://AzureADTokenExchange']
  }
}]
