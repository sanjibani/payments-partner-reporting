# Azure deployment

## One-time setup

```bash
# 1. Login and pick the resource group
az login
az group create --name rg-payrep-prod --location eastus

# 2. Create the Container App + registry + log analytics from the bicep
az deployment group create \
  --resource-group rg-payrep-prod \
  --template-file azure/main.bicep \
  --parameters containerImage=<your-acr>.azurecr.io/payments-partner-reporting:0.1.0
```

## Build and push the image

```bash
az acr build \
  --registry <your-acr> \
  --image payments-partner-reporting:0.1.0 \
  .
```

## Wire the weekly trigger

Create a Logic App with the workflow in `logic-app-workflow.json`. It
runs every Monday 06:00 UTC and POSTs to the Container App's
`/run-weekly` endpoint. When the run fails, the Logic App hits a
secondary webhook (your alerting channel) with the error body.

## Cost summary

| Resource                  | Idle cost                  | Weekly cost       |
|---------------------------|----------------------------|-------------------|
| Container App (minRepl=0) | $0                         | ~$0.001 per run   |
| Container Registry (Basic)| ~$0.167 / day              | same              |
| Log Analytics (30d ret.)  | ~$0.10 / GB ingested       | < $0.01 / week    |
| Application Insights      | ~$2.30 / GB ingested       | depends on volume |

When the Container App is scaled to zero (between weekly runs), there
are no compute charges. The only always-on spend is ACR + a small slice
of Log Analytics ingestion.

## Secrets

The bicep template references a secret named `llm-api-key`. Set it via:

```bash
az containerapp secret set \
  --name ca-payrep-prod \
  --resource-group rg-payrep-prod \
  --secrets llm-api-key=$LLM_API_KEY
```