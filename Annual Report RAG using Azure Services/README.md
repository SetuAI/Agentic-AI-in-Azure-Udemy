# Annual Report RAG on Azure

Ask how something changed between two years of a company's annual report, and
get an answer that is checked before anyone sees it. If the answer can't be
verified, the pipeline **pauses and asks a person** to approve, correct or reject it.

Use it from the **terminal** (`run_query.py`) or from a **web UI** (`UI/`) that
shows what search found, how the semantic ranker re-ordered it, and the final answer.

This is the Azure version of the project. It is **completely separate** from the
OpenAI/Chroma version: nothing is shared, and nothing here changes it.

| Job                          | Service                                  |
| ---------------------------- | ---------------------------------------- |
| Store the PDFs               | Azure Blob Storage                       |
| Read the PDFs (text, tables) | PyMuPDF4LLM (free, runs locally)         |
| Store and search the chunks  | Azure AI Search (hybrid: keyword + vector) |
| Pick the best 5 chunks       | Azure AI Search semantic ranker          |
| Embeddings, answers, checks  | Azure OpenAI                             |
| Steps, routing, review pause | LangGraph                                |

PDFs are converted with **PyMuPDF4LLM** rather than Azure AI Document
Intelligence, which bills per page and would be costly for reports this long.

---

## How it works, end to end

```
                         ┌───────────────────── INGESTION (once) ─────────────────────┐
 data/raw/*.pdf ──upload──► Blob Storage ──► PyMuPDF4LLM ──► data/processed/*.md
                                                                   │
                                                  chunks (1,200 chars, 200 overlap,
                                                  labelled company + fiscal_year)
                                                                   │
                                      Azure OpenAI embeddings (512 dims) ──► AI Search index
                         └────────────────────────────────────────────────────────────┘

                         ┌────────────────────── QUERY (every question) ──────────────┐
 question + company
   │
   ▼
 input guardrails ── refused? ──► polite refusal
   │  (personal data, investment advice, off-topic)
   ▼
 hybrid search, once PER YEAR (filter: company + fiscal_year) ──► 20 candidates / year
   ▼
 semantic ranker ──► best 5 / year
   ▼
 generate (gpt-4o): what FY2025 said · what FY2026 said · what changed
   ▼
 output guardrails: right shape? every figure traced to the right year's chunks?
   │
   ├── grounded ─────────────► answer shown
   └── can't verify ─────────► PAUSE → human approves / corrects / rejects
                         └────────────────────────────────────────────────────────────┘
```

Why search **each year separately**: the two reports use almost identical wording,
so an unfiltered search mixes years. Filtering by `fiscal_year` guarantees both
sides of the comparison get their own evidence.

---

## What goes in, what comes out

**In:** a question and a company (`Infosys` or `Eternal`).

**Out:** one of three things.
- **An answer** in three parts: what FY2025 said, what FY2026 said, what changed.
- **A refusal**, for off-topic questions, investment advice, or questions with personal data.
- **A pause for review**, when a figure or claim can't be traced to the right year's report.

---

## Setup (once)

### 1. Install

Python 3.12 is recommended.

```bash
cd "Annual Report RAG using Azure Services"
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create the Azure resources

Needs the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).

```bash
az login
bash azure_setup/provision.sh
```

It takes about 10 minutes and ends by printing a block of settings.
(Already have the resources? Skip this and fill `.env` by hand from the portal.)

### 3. Save the settings

```bash
cp .env.example .env
```

Paste the block that `provision.sh` printed into `.env`. Three values are easy to get wrong:

| Setting | Correct | Wrong |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | `https://<name>.openai.azure.com/` | `.../openai/v1` on the end → **404 Resource not found** |
| `AZURE_OPENAI_API_VERSION` | a REST API version, e.g. `2024-10-21` | a model version such as `2024-11-20` |
| `AZURE_STORAGE_CONNECTION_STRING` | the full string: `DefaultEndpointsProtocol=https;AccountName=…;AccountKey=…;EndpointSuffix=core.windows.net` | the account key alone → **Connection string missing required connection details** |

Get the connection string from **Storage account → Security + networking → Access keys → Connection string**
(not the *Key* field), or:

```bash
az storage account show-connection-string -n <storage-account> -g <resource-group> -o tsv
```

`.env` holds keys and is git-ignored. Never commit it.

### 4. Put the four PDFs in `data/raw/`

```
data/raw/infosys_fy2025.pdf
data/raw/infosys_fy2026.pdf
data/raw/eternal_fy2025.pdf
data/raw/eternal_fy2026.pdf
```

The names must match `DOCUMENTS` in `config.py`. `LOCAL_PDF_DIR` in `.env`
(default here: `./data/raw`) tells the uploader where to look. The PDFs are git-ignored.

### 5. Upload them, then build the index

```bash
python ingestion/upload_pdfs.py    # data/raw -> Blob Storage
python run_ingestion.py            # PDFs -> markdown -> chunks -> embeddings -> AI Search
```

`run_ingestion.py` does three steps:

1. **PDF → markdown.** Converts each report with PyMuPDF4LLM and saves it to
   `data/processed/<Company>_<FY>.md`. Already-converted reports are read from
   there, so this is slow only the first time (large PDFs take minutes each).
   A PDF missing from `data/raw/` is downloaded from Blob Storage first.
2. **Markdown → chunks.** ~1,200 characters each, cut at headings and paragraphs
   where possible, every chunk labelled with its company and fiscal year.
3. **Chunks → AI Search.** **Deletes and recreates** the `annual-reports` index,
   embeds every chunk (100 per call) and uploads them (500 per batch).

With the four sample reports this produces about **5,150 chunks** and takes
around **3–4 minutes** once the markdown is cached.

**Check it in the portal:** AI Search service → **Indexes → `annual-reports`**.
The document count should match the chunk total. In **Search explorer**, JSON view:

```json
{
  "search": "*",
  "filter": "company eq 'Infosys' and fiscal_year eq 'FY2025'",
  "select": "id,company,fiscal_year,position,text",
  "top": 5,
  "count": true
}
```

---

## Ask questions

### Option A: the web UI

```bash
python UI/server.py                # then open http://localhost:8000
python UI/server.py 8080           # another port
```

Type a question in the box at the top, pick **Infosys** or **Eternal**, press
**Ask** (or ⌘/Ctrl + Enter). A question takes about 10–30 seconds. The page shows:

- **Pipeline strip:** guardrails → hybrid search → semantic ranker → generate →
  grounding → answer, each marked passed, flagged or failed.
- **Answer card:** the answer, a *grounded / needs review / ungrounded* badge,
  the share of figures that traced back to the sources, and anything flagged.
- **Human review:** if the answer can't be verified, the draft is shown with
  **Approve** / **Reject** buttons and a box for a corrected answer.
- **Retrieval, per year (FY2025 / FY2026 tabs):**
  - *Before*: all 20 hybrid-search candidates with their scores; dropped ones greyed.
  - *How positions changed*: a slope chart from hybrid rank to semantic rank
    (green = promoted, red = demoted, dashed = dropped).
  - *After*: the 5 kept chunks with the semantic score (0–4), how many places
    each moved, and its original hybrid rank.
  - Hover any chunk to highlight it everywhere; click to read the full text.

`UI/server.py` uses only Python's standard library and calls the same
`build_graph()` as the terminal version. Paused reviews live in the server's
memory, so restarting it discards any unfinished review.

### Option B: the terminal

```bash
python run_query.py                                            # default question
python run_query.py "How did attrition change?"                # your question, Infosys
python run_query.py "How did revenue change?" Eternal          # your question, Eternal
```

If the answer needs review, the terminal asks you to approve (`a`) or reject (`r`)
and optionally type a corrected answer.

### Each step on its own

Every step also runs by itself, which is how the course teaches it:

```bash
python guardrails/safety.py        # what gets refused (free)
python guardrails/topic.py         # off-topic check (keyword: free; otherwise 1 embedding)
python guardrails/format.py        # badly shaped answers (free)
python guardrails/factual.py       # wrong-year figures caught (free)
python retrieval/search.py         # hybrid search, one year at a time
python retrieval/rerank.py         # what the semantic ranker changed
python graph/build_graph.py        # the pipeline as a diagram (paste into mermaid.live)
```

---

## Key settings (`config.py`)

| Setting | Value | Meaning |
| --- | --- | --- |
| `EMBEDDING_DIMENSIONS` | `512` | Vector length requested from `text-embedding-3-small` (native 1,536). Smaller vectors use a third of the index storage. **Change it → re-run `run_ingestion.py`**, because the index and every vector must match. |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1200` / `200` | Characters per chunk / repeated between neighbours |
| `MIN_CHUNK_LENGTH` | `100` | Shorter pieces (page numbers, stray headers) are dropped |
| `VECTOR_SEARCH_TOP_K` | `20` | Candidates fetched per year by hybrid search |
| `RERANK_TOP_K` | `5` | Chunks kept per year after the semantic ranker |

---

## Tests

```bash
python -m pytest tests
```

All offline: no Azure account, no keys, no cost. They cover the search filter,
the index definition, the search call, the semantic rerank, the PDF-to-markdown
cache, the content-filter handling, the guardrails, and the review pause.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `ValueError: Connection string missing required connection details` | `AZURE_STORAGE_CONNECTION_STRING` holds only the key. Paste the full connection string. |
| `openai.NotFoundError: 404 Resource not found` | Endpoint has `/openai/v1` on the end, the API version is wrong, or the deployment name in `.env` doesn't exist. |
| `data/processed/` is empty | Nothing is written there until `run_ingestion.py` finishes converting a report. Large PDFs take minutes. |
| Vector dimension mismatch on upload or search | `EMBEDDING_DIMENSIONS` changed without rebuilding. Run `python run_ingestion.py`. |
| `WARNING: no semantic ranker scores` | Enable the semantic ranker on the AI Search service (Free plan is fine). |
| `pip` warns about `crewai-tools` / `pymupdf` | Another project in the same Python. Harmless here; use a `.venv` to keep them apart. |

---

## Cost: read before running

- **AI Search Basic** is billed **per hour** while it exists, even when idle.
- **PDF conversion is free.** PyMuPDF4LLM runs on your machine and caches results in `data/processed/`.
- **Azure OpenAI** is billed per token. Building the index embeds every chunk once;
  each question costs a few calls.

When you're done:

```bash
bash azure_setup/teardown.sh       # deletes everything, stops all charges
```

---

## Folder layout

```
config.py                 every setting (secrets come from .env)
azure_clients.py          one connection per Azure service
run_ingestion.py          build the search index
run_query.py              ask a question in the terminal, review if needed

data/                     git-ignored
  raw/                    the four PDFs
  processed/              markdown cache, one file per report

ingestion/
  upload_pdfs.py          PDFs -> Blob Storage
  load_pdfs.py            PDF -> PyMuPDF4LLM -> markdown (cached)
  chunk.py                markdown -> labelled chunks
  build_index.py          chunks -> embeddings -> Azure AI Search index

retrieval/
  search.py               hybrid search + semantic ranker, filtered by year
  rerank.py               keep the 5 best by semantic score

guardrails/
  safety.py               input:  personal data? advice request?
  topic.py                input:  about the annual reports?
  format.py               output: compares both years, no advice?
  factual.py              output: every figure in the right year's sources?

graph/
  state.py                what flows through
  nodes.py                what each step does
  build_graph.py          the order, and the pause for review

UI/
  server.py               local web server: runs the graph, serves the page
  index.html              the page: query box, answer, review, rerank chart

azure_setup/
  provision.sh            create every Azure resource
  teardown.sh             delete them all

tests/                    offline tests
```

---

## Suggested course order

1. **Azure OpenAI.** Deployments vs model names; the `AzureOpenAI` client.
2. **Blob Storage + PyMuPDF4LLM.** Storing files in Blob; turning PDFs into markdown for free (and why Document Intelligence was skipped: per-page cost).
3. **Azure AI Search.** Index fields, filters, hybrid search, and why the year filter is still essential.
4. **Semantic ranker.** What it changes (`retrieval/rerank.py` and the UI's slope chart show each chunk's move).
5. **Guardrails and human review.** Content filter errors, grounding, and the LangGraph pause.

---

## Model versions and regions

`provision.sh` deploys `gpt-4o` (2024-11-20), `gpt-4o-mini` (2024-07-18) and
`text-embedding-3-small` (1) with the `GlobalStandard` SKU in `eastus2`. Azure
retires model versions and quota varies by region. If a deployment fails, check
what your subscription offers in the Azure AI Foundry portal, then change
`LOCATION` or the versions at the top of the script.
