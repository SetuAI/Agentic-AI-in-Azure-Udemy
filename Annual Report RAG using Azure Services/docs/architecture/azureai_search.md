# Azure AI Search: setup for the Annual Report RAG project

## What Azure AI Search does in this project

Azure AI Search holds **every chunk of the 4 annual reports**, ready to be searched. It is used at **both** stages:

```
BUILD TIME (once, from your laptop)
  ingestion/build_index.py
      │  creates the index "annual-reports" (fields + vector setup + semantic setup)
      │  uploads every chunk: its text, its labels, and its 512-number embedding
      ▼
  Azure AI Search  ·  index: annual-reports

RUN TIME (every question)
  graph step 2 · retrieve  (retrieval/search.py)
      │  one search per year, filtered to one company + one year
      │  hybrid search: keywords + vectors  →  20 chunks per year
      │  semantic ranker scores those 20 in the same call
      ▼
  graph step 3 · rerank  (retrieval/rerank.py)
      keeps the best 5 per year, using the semantic ranker's score
```

**Why Azure AI Search, and not a plain vector database?** One service does all four jobs this project needs:

| Job                       | What it means here                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Keyword search**  | Finds chunks that contain the exact words, e.g. "attrition" or a specific figure                             |
| **Vector search**   | Finds chunks that mean the same as the question, even with different words                                   |
| **Filters**         | Only Infosys, only FY2025. This is how we keep the two years apart                                           |
| **Semantic ranker** | Reads the question and each chunk together and scores how well the chunk answers it. That's how 20 becomes 5 |

**What we need from it:** one search service (Basic tier), the semantic ranker on its Free plan (the default), and its endpoint and admin key in `.env`. **We do not create the index in the portal.** `build_index.py` creates it from code.

---

## Before you start

| Item           | Value                                                            |
| -------------- | ---------------------------------------------------------------- |
| Resource group | `annual-rg-1`                                                  |
| Region         | **East US 2** (same as Azure OpenAI and Blob Storage)      |
| Service name   | `annual-report-search-1`                                       |
| Portal path    | Azure portal → search **AI Search** → **+ Create** |

---

## Tab 1 · Basics

| Setting        | Value                                                                           | Why                                                                                                                  |
| -------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Subscription   | your subscription                                                               |                                                                                                                      |
| Resource group | **annual-rg-1**                                                           | Keeps everything for this project in one place                                                                       |
| Service name   | **annual-report-search-1**                                                | Lowercase letters, numbers and dashes. It becomes the address: `https://annual-report-search-1.search.windows.net` |
| Location       | **East US 2**                                                             | Same region as the other services                                                                                    |
| Pricing tier   | **Basic** → click **Change Pricing Tier** if it shows another tier | See the table below                                                                                                  |

### Why Basic

| Tier               | Use it?       | Why                                                                                                                                        |
| ------------------ | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Free               | No            | Azure may delete it after a period of inactivity; it runs on shared machines; you can have only one per subscription; the storage is small |
| **Basic**    | **Yes** | A dedicated service with hybrid search, filters, the semantic ranker and query keys. Plenty of room for 4 reports                          |
| Standard and above | No            | Built for far bigger indexes and traffic. Several times the price                                                                          |

Basic is charged **for every hour it exists**, even when nobody searches. Delete it when you're finished (see the end of this file).

---

## Tab 2 · Scale

| Setting    | Value       | Why                                                                                 |
| ---------- | ----------- | ----------------------------------------------------------------------------------- |
| Replicas   | **1** | Copies of the service that handle more searches at once. One is enough for a course |
| Partitions | **1** | Splits of the storage. One Basic partition holds far more than 4 reports            |

Each extra replica or partition adds the full Basic price again.

---

## Tab 3 · Networking

| Setting                      | Value            | Why                                                                                  |
| ---------------------------- | ---------------- | ------------------------------------------------------------------------------------ |
| Endpoint connectivity (data) | **Public** | Your laptop (ingestion) and the web app on App Service both need to reach it         |
| Private endpoint             | **none**   | That is for traffic inside an Azure virtual network, which this project doesn't have |

"Public" means the service's address can be reached. Nothing can be searched or changed without a key.

---

## Tab 4 · Tags (optional)

| Name    | Value             |
| ------- | ----------------- |
| project | annual-report-rag |

## Tab 5 · Review + create

Check the summary, then click **Create**. It takes a few minutes.

---

## After it is created

### Step 1 · Check the semantic ranker plan (usually nothing to do)

Search service → **Settings** → **Semantic ranker**

New search services start on the **Free** plan by default, so this should already show **Free**. You are only checking that it is **not Disabled**.

| What you see   | What to do                                     | Why                                                                                          |
| -------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **Free** | **Nothing.** Move on                     | A monthly allowance of semantic ranker requests at no extra charge. Enough for the course    |
| Disabled       | Select **Free** → **Select plan** | When it's Disabled, every search fails, because our code always asks for the semantic ranker |
| Standard       | Change it to **Free**                    | Standard charges for every request                                                           |

**This plan is separate from the Basic pricing tier.** The tier is the service; this plan is only for the semantic ranker inside it.

The other two parts of the semantic ranker are done by the code, not in the portal:

- `ingestion/build_index.py` creates the index's semantic configuration, `annual-reports-semantic`, which tells the ranker to read the `text` field.
- `retrieval/search.py` asks for the ranker in every search (`query_type="semantic"`).

### Step 2 · Check that API keys are allowed

Search service → **Settings** → **Keys** → **API access control**

Choose **API keys** (or **Both**). Our code connects with a key. If it is set to "Role-based access control" only, every request fails with `403`.

### Step 3 · Copy the endpoint and admin key into `.env`

- **Endpoint:** Search service → **Overview** → **Url**
- **Key:** Search service → **Settings** → **Keys** → **Primary admin key** → copy

```dotenv
# --- Azure AI Search ---
AZURE_SEARCH_ENDPOINT=https://annual-report-search-1.search.windows.net
AZURE_SEARCH_API_KEY=<primary admin key>
AZURE_SEARCH_INDEX=annual-reports
```

| Setting                   | Notes                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------------------------------------- |
| `AZURE_SEARCH_ENDPOINT` | Ends at `.net`, with nothing after it                                                                 |
| `AZURE_SEARCH_API_KEY`  | The **admin** key, because ingestion creates the index and uploads to it. Never show it on screen |
| `AZURE_SEARCH_INDEX`    | The name `build_index.py` gives the index. Leave it as `annual-reports`                             |

### Step 4 · Build the index

Blob Storage must already hold the 4 PDFs (see `blob_storage.md`). Then, from the project folder:

```bash
python run_ingestion.py
```

It runs three steps: PDFs to markdown, markdown to chunks, then chunks to the index. The last step creates the index and uploads every chunk with its embedding.

### Step 5 · Check it in the portal

Search service → **Search management** → **Indexes** → **annual-reports**

| Check                                 | What you should see                                                                                          |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Document count                        | One document per chunk. The two years of one company should be broadly similar                               |
| **Fields** tab                  | `id`, `text`, `company`, `fiscal_year`, `year_label`, `source_file`, `position`, `embedding` |
| **Vector profiles** tab         | One profile using **HNSW**                                                                             |
| **Semantic configurations** tab | `annual-reports-semantic`, reading the `text` field                                                      |
| **Search explorer**             | Type `attrition` and press Search. You get chunks containing that word                                     |

---

## What the index contains (created by `build_index.py`)

| Field           | Type                | Used for                                                                     |
| --------------- | ------------------- | ---------------------------------------------------------------------------- |
| `id`          | text (key)          | A unique id per chunk, e.g. `Infosys_FY2025_42`                            |
| `text`        | text, searchable    | The chunk itself. Keyword search reads this, and so does the semantic ranker |
| `company`     | text, filterable    | Filter: one company per search                                               |
| `fiscal_year` | text, filterable    | Filter: one year per search                                                  |
| `year_label`  | text                | Shown on the page, e.g. "FY 2024-25"                                         |
| `source_file` | text                | Which PDF the chunk came from                                                |
| `position`    | number              | Where in the report the chunk sits                                           |
| `embedding`   | vector, 512 numbers | Vector search. Made with text-embedding-3-small                              |

Two settings stored in the index:

| Setting                | Value                             | What it does                                                                 |
| ---------------------- | --------------------------------- | ---------------------------------------------------------------------------- |
| Vector search method   | **HNSW**                    | A fast way to find the nearest vectors without comparing against every chunk |
| Semantic configuration | **annual-reports-semantic** | Tells the semantic ranker to read the `text` field                         |

---

## The read-only key for the web app (later, in `webapps.md`)

The web app only **searches**. It never creates or changes the index. So it gets a **query key**, which can search but cannot change or delete anything:

```bash
az search query-key create --service-name annual-report-search-1 --resource-group annual-rg-1 -n web-app
```

`webapps.md`, Step 15, puts a query key in the web app's settings. **Your laptop keeps the admin key**, because `run_ingestion.py` needs it.

| Who                                | Key                 | Can do                                  |
| ---------------------------------- | ------------------- | --------------------------------------- |
| Your laptop (`run_ingestion.py`) | **Admin key** | Create the index, upload chunks, search |
| The web app on App Service         | **Query key** | Search only                             |

---

## If something goes wrong

| Error                                                               | Cause                                                    | Fix                                                                                                    |
| ------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `These settings are missing from .env: AZURE_SEARCH_ENDPOINT`     | `.env` not filled in                                   | Step 3                                                                                                 |
| `403 Forbidden`                                                   | API keys not allowed, or a query key used for ingestion  | Step 2; use the **admin** key in `.env`                                                        |
| `404` / `index not found` when asking a question                | The index hasn't been built yet                          | Step 4                                                                                                 |
| An error mentioning **semantic** (e.g. not enabled, or quota) | Semantic ranker is off, or the free allowance is used up | Step 1; the free allowance resets each month                                                           |
| An error about the **vector dimensions**                      | The index was built with a different embedding size      | Delete the index in the portal and run `python run_ingestion.py` again. The code uses 512 throughout |

---

## When you are finished

Basic is charged every hour, even when idle. Delete it when you're done:

- **Just this service:** Search service → **Overview** → **Delete**
- **The whole project:** `bash azure_setup/teardown.sh`, which deletes the resource group `annual-rg-1`
