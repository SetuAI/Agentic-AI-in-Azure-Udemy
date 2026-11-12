
# Azure Blob Storage: setup for the Annual Report RAG project

## What Blob Storage does in this project

Blob Storage keeps the **4 annual report PDFs** in the cloud:

```
infosys_fy2025.pdf    infosys_fy2026.pdf
eternal_fy2025.pdf    eternal_fy2026.pdf
```

It is used **only at build time**, never when a user asks a question:

```
your laptop (data/raw/)
      │  ingestion/upload_pdfs.py          uploads the 4 PDFs
      ▼
Blob Storage  (container: annual-reports)
      │  ingestion/load_pdfs.py            downloads them again to build the index
      ▼
data/raw/ → markdown → chunks → Azure AI Search
```

**Why keep the PDFs in the cloud if they are already on the laptop?** So the index can be rebuilt from the same files by anyone on the team, or from any machine, not only from the laptop that happens to have them.

**What we need from it:** one storage account, one private container called `annual-reports`, and its **connection string** in `.env`.

---

## Before you start

| Item           | Value                                                                   |
| -------------- | ----------------------------------------------------------------------- |
| Resource group | `annual-rg-1`                                                         |
| Region         | **East US 2** (same as Azure OpenAI and AI Search)                |
| Portal path    | Azure portal → search **Storage accounts** → **+ Create** |

---

## Tab 1 · Basics

| Setting              | Value                                                         | Why                                                                                                    |
| -------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Subscription         | your subscription                                             |                                                                                                        |
| Resource group       | **annual-rg-1**                                         | Keeps everything for this project in one place                                                         |
| Storage account name | e.g. `annualreportstore01`                                  | Lowercase letters and numbers only, 3–24 characters. Must be unique across all of Azure               |
| Region               | **East US 2**                                           | Same region as the other services                                                                      |
| Primary service      | **Azure Blob Storage or Azure Data Lake Storage Gen 2** | We store files (blobs)                                                                                 |
| Performance          | **Standard**                                            | Premium is for very heavy traffic. We store 4 PDFs                                                     |
| Redundancy           | **LRS** (locally-redundant storage)                     | The cheapest option: 3 copies in one data centre. If it were ever lost, we would upload the PDFs again |

---

## Tab 2 · Advanced

| Setting                                                                                     | Value              | Why                                                                                    |
| ------------------------------------------------------------------------------------------- | ------------------ | -------------------------------------------------------------------------------------- |
| Enable hierarchical namespace                                                               | **Off**      | Turning it on makes the account a Data Lake Gen2. We only need one plain container     |
| Enable network file system v3                                                               | Off (greyed out)   | Not used                                                                               |
| Enable SFTP                                                                                 | Off (greyed out)   | Not used                                                                               |
| Allow cross-tenant replication                                                              | **Off**      | Nothing is copied to other Azure tenants                                               |
| Access tier                                                                                 | **Hot**      | Our files are small. Cool and Cold cost less to keep but more each time a file is read |
| **Azure Files** section (Managed Identity for SMB, Encryption in Transit for SMB/NFS) | leave the defaults | We don't use Azure Files, so these have no effect                                      |

---

## Tab 3 · Networking

| Setting                     | Value                               | Why                                                                                                                        |
| --------------------------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Public network access       | **Enable**                    | Your laptop has to reach the account to upload and download                                                                |
| Public network access scope | **Enable from all networks**  | Your laptop's IP address changes between networks. With "selected networks", uploads fail with `403` whenever it changes |
| Private endpoint            | **none**                      | That is for traffic inside an Azure virtual network, which this project doesn't have                                       |
| Routing preference          | **Microsoft network routing** | Recommended default                                                                                                        |

**About the red warning on this tab:** "public" means anyone can *reach* the account's address. It does **not** mean anyone can *read* the files. Every request still needs the key, and the key is only in your `.env`.

---

## Tab 4 · Data protection

| Setting                                     | Value                | Why                                                          |
| ------------------------------------------- | -------------------- | ------------------------------------------------------------ |
| Enable point-in-time restore for containers | Off                  | Not needed for 4 fixed files                                 |
| **Enable soft delete for blobs**      | **On, 7 days** | A deleted PDF can be brought back within 7 days. Almost free |
| **Enable soft delete for containers** | **On, 7 days** | The same for the whole container                             |
| Enable soft delete for file shares          | leave as is          | We don't use file shares                                     |
| Enable versioning for blobs                 | Off                  | The PDFs never change                                        |
| Enable blob change feed                     | Off                  | Not used                                                     |
| Enable version-level immutability support   | Off                  | Not used                                                     |

---

## Tab 5 · Security

| Setting                                                      | Value                              | Why                                                                                                                                                     |
| ------------------------------------------------------------ | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Require secure transfer for REST API operations              | **On**                       | HTTPS only                                                                                                                                              |
| Allow enabling anonymous access on individual containers     | **Off**                      | Nobody can read a file without a key                                                                                                                    |
| **Enable storage account key access**                  | **On** ⚠️                  | **Required.** Our code connects with the connection string, and the connection string contains this key. If this is off, `upload_pdfs.py` fails |
| Default to Microsoft Entra authorization in the Azure portal | Off                                | Not needed                                                                                                                                              |
| Minimum TLS version                                          | **Version 1.2**              | Current secure standard                                                                                                                                 |
| Permitted scope for copy operations                          | From any storage account (default) | Not used                                                                                                                                                |

---

## Tab 6 · Encryption

| Setting                                  | Value                                    | Why                                                         |
| ---------------------------------------- | ---------------------------------------- | ----------------------------------------------------------- |
| Encryption type                          | **Microsoft-managed keys (MMK)**   | Files are always encrypted on disk. Azure manages the key   |
| Enable support for customer-managed keys | **Blobs and files only** (default) | Can't be changed later, and we never need it                |
| Enable infrastructure encryption         | **Off**                            | A second layer of encryption that this project doesn't need |

Customer-managed keys need an Azure Key Vault and extra setup. They are for companies whose rules require them.

---

## Tab 7 · Tags (optional)

| Name    | Value             |
| ------- | ----------------- |
| project | annual-report-rag |

Tags let you filter costs by project later.

## Tab 8 · Review + create

Check the summary, then click **Create**. It takes under a minute.

---

## After it is created

### Step 1 · Create the container

Storage account → **Data storage** → **Containers** → **+ Container**

| Setting                | Value                                   | Why                                                |
| ---------------------- | --------------------------------------- | -------------------------------------------------- |
| Name                   | **`annual-reports`**            | Must match `AZURE_STORAGE_CONTAINER` in `.env` |
| Anonymous access level | **Private (no anonymous access)** | The files can only be read with the key            |

### Step 2 · Copy the connection string into `.env`

Storage account → **Security + networking** → **Access keys** → **key1** → **Connection string** → **Show** → **Copy**

```dotenv
# --- Azure Blob Storage ---
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net
AZURE_STORAGE_CONTAINER=annual-reports

# --- Where upload_pdfs.py finds the four PDFs on your computer ---
LOCAL_PDF_DIR=./data/raw
```

The connection string holds the account name **and** its key. Never paste it into any other file, and never show it on screen.

### Step 3 · Test it

Put the 4 PDFs in `data/raw/`, then run from the project folder:

```bash
python ingestion/upload_pdfs.py
```

**Expected:** one line per PDF saying it was uploaded. In the portal, open **Containers → annual-reports** and you will see the 4 files.

---

## If something goes wrong

| Error                                                                     | Cause                                                                           | Fix                                                                               |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `These settings are missing from .env: AZURE_STORAGE_CONNECTION_STRING` | `.env` not filled in                                                          | Step 2                                                                            |
| `AuthorizationFailure` / `403`                                        | Storage account key access is off, or "selected networks" is on without your IP | Security tab → key access **On**; Networking → **all networks**     |
| `ContainerNotFound`                                                     | The container name doesn't match                                                | The container must be `annual-reports`, the same as `AZURE_STORAGE_CONTAINER` |
| `SKIPPED infosys_fy2025.pdf (not found in ...)`                         | The PDF isn't in `data/raw/`                                                  | Check the file name and `LOCAL_PDF_DIR`                                         |

---

## Who uses this storage account

| Who                                        | Uses it?     | How                                                                                           |
| ------------------------------------------ | ------------ | --------------------------------------------------------------------------------------------- |
| `ingestion/upload_pdfs.py` (your laptop) | Yes          | Uploads the 4 PDFs                                                                            |
| `ingestion/load_pdfs.py` (your laptop)   | Yes          | Downloads them to build the index                                                             |
| The web app on App Service                 | **No** | It only searches the index, so it is never given the storage key (see `webapps.md`, Step 12) |
