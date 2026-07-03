name: payments-partner-reporting

# Bicep template for Azure Container Apps.
# Provisions:
#   - Azure Container Registry
#   - Log Analytics workspace (for ACA diagnostics)
#   - Container Apps Environment
#   - Container App with minReplicas=0 (scale to zero)
#
# Cost when idle: storage + ACR private endpoint, no compute.
# Cost during weekly job: ~1 vCPU * 60-180s, pennies.

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Environment name suffix')
param envSuffix string = 'prod'

@description('Container image, e.g. myacr.azurecr.io/payments-partner-reporting:0.1.0')
param containerImage string

@description('Container App ingress FQDN (for Logic App trigger)')
param ingressFqdn string = ''

var acrName = 'acrpayrep${uniqueString(resourceGroup().id)}'
var lawName = 'law-payrep-${envSuffix}'
var caeName = 'cae-payrep-${envSuffix}'
var caName = 'ca-payrep-${envSuffix}'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource law 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: lawName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource cae 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: caeName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        // sharedKey is set via key vault / listKeys in real deployments
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

resource ca 'Microsoft.App/containerApps@2023-05-01' = {
  name: caName
  location: location
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        allowInsecure: false
        fqdn: ingressFqdn
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: 'system'
        }
      ]
      // Scale to zero when no traffic.
      minReplicas: 0
      maxReplicas: 2
    }
    template: {
      containers: [
        {
          image: containerImage
          name: 'payments-reporting'
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: [
            {
              name: 'LLM_API_KEY'
              secretRef: 'llm-api-key'
            }
            {
              name: 'LLM_BASE_URL'
              value: 'https://api.minimax.io/v1'
            }
            {
              name: 'LLM_MODEL'
              value: 'MiniMax-Text-01'
            }
            {
              name: 'EMAIL_DRY_RUN'
              value: '1'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '1'
              }
            }
          }
        ]
      }
    }
  }
}

output containerAppFqdn string = ca.properties.configuration.ingress.fqdn
output containerRegistryLoginServer string = acr.properties.loginServer