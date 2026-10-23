# Day 1: Azure setup

Goal for today: an empty but correctly structured Azure environment, with a
cost guardrail in place **before** anything that costs money exists.

Estimated time: 1.5-2 hours (plus ~2 hours of AZ-900 study).

---

## 0. Before you start

- Create an Azure free account (or use an existing subscription).
- Install the Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli
- Sign in:

```bash
az login
az account show --output table      # confirm the right subscription
```

Pick a short unique suffix for globally unique names (storage accounts and key
vaults must be unique across all of Azure), e.g. your initials + digits:

```bash
SUFFIX=yp2610                 # change this
RG=rg-ontario-grid
LOC=canadacentral
SA=stontgrid$SUFFIX           # 3-24 chars, lowercase letters/numbers only
KV=kv-ontgrid-$SUFFIX
```

## 1. Budget alert FIRST (portal)

Cost Management + Billing → Budgets → **Add**:
- Scope: your subscription
- Amount: a small monthly number you are comfortable with
- Alerts: 50%, 80%, 100% of actual cost → your email

> AZ-900 link: this is the *Cost Management* objective. Note what a budget does
> and does **not** do (it alerts; it does not stop resources).

## 2. Resource group

```bash
az group create --name $RG --location $LOC
```

Everything in this project lives in this one group, so deleting the group
deletes the whole project cleanly.

## 3. Data lake (ADLS Gen2)

`--hns true` (hierarchical namespace) is what turns a normal storage account
into a Data Lake Gen2 account with real folders.

```bash
az storage account create \
  --name $SA --resource-group $RG --location $LOC \
  --sku Standard_LRS --kind StorageV2 --hns true \
  --min-tls-version TLS1_2 --allow-blob-public-access false
```

Give yourself data access (control-plane "Owner" is not the same as data access):

```bash
ME=$(az ad signed-in-user show --query id -o tsv)
SA_ID=$(az storage account show -n $SA -g $RG --query id -o tsv)
az role assignment create --assignee $ME \
  --role "Storage Blob Data Contributor" --scope $SA_ID
```

Role assignments can take a few minutes to apply. Then create the layers:

```bash
for c in bronze silver gold quarantine meta; do
  az storage fs create --name $c --account-name $SA --auth-mode login
done
```

| Container   | Holds                                                   |
|-------------|---------------------------------------------------------|
| bronze      | raw IESO files, exactly as downloaded                   |
| silver      | clean Delta tables                                      |
| gold        | star schema + marts                                     |
| quarantine  | rejected rows with a reason                             |
| meta        | file manifest + audit log                               |

## 4. Key Vault

```bash
az keyvault create --name $KV --resource-group $RG --location $LOC
```

Nothing goes in it yet. Later it will hold the Databricks token that Data
Factory uses, so no secret ever appears in code or pipeline JSON.

## 5. Explore the source by hand (30 min)

Open these in a browser and look at the files yourself:

- https://reports-public.ieso.ca/public/Demand/
- https://reports-public.ieso.ca/public/GenOutputbyFuelHourly/
- https://reports-public.ieso.ca/public/DAHourlyOntarioZonalPrice/

Notice: the `_v<N>` versions, the "alias" files without a version, and the
day (Sep 24 2026) that has v1, v2 **and** v3. That is the revision problem
your pipeline solves.

## 6. Run the local pipeline

From the repo root on your laptop:

```bash
python -m pip install pytest
python -m pytest            # 21 tests should pass
python scripts/run_local.py # downloads real IESO data into ./data
python scripts/run_local.py # run again: 0 new files (idempotent)
```

Open `data/silver/*.csv` and `data/audit_log.csv`.

## End-of-day checklist

- [ ] Budget alert created
- [ ] Resource group, storage account (HNS on), 5 containers, Key Vault
- [ ] Tests pass locally
- [ ] `run_local.py` ran twice; second run downloaded nothing
- [ ] Repo pushed to GitHub (`data/` is git-ignored)

## Cost notes

Storage and Key Vault at this size cost very little. The expensive pieces
come later (Databricks clusters). Rule for the whole project: **terminate
clusters when not in use and set auto-termination to 15-20 minutes.**
