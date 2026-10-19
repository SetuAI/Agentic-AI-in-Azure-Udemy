# Annual Report RAG on Azure

Ask how something changed between two years of a company's annual report, and
get an answer that is checked before anyone sees it. If the answer can't be
verified, the pipeline **pauses and asks a person** to approve, correct or reject it.

This is the Azure version of the project. It is **completely separate** from the
OpenAI/Chroma version: nothing is shared, and nothing here changes it.

| Job                        | Azure service                    |
| -------------------------- | -------------------------------- |
| Store the PDFs             | Azure Blob Storage               |
| Read the PDFs (text, tables)  | PyMuPDF4LLM (free, runs locally) |
| Store and search the chunks | Azure AI Search (hybrid search) |
| Pick the best 5 chunks     | Azure AI Search semantic ranker  |
| Embeddings, answers, checks | Azure OpenAI                    |

PDFs are converted with **PyMuPDF4LLM** rather than Azure AI Document
Intelligence, which bills per page and would be costly for reports this long.

---

## What goes in, what comes out

**In:** a question and a company (`Infosys` or `Eternal`).

```bash
python run_query.py "How did revenue change between the two years?" Infosys
```

**Out:** one of three things.
- **An answer** in three parts: what FY2025 said, what FY2026 said, what changed.
- **A refusal**, for off-topic questions, investment advice, or questions with personal data.
- **A pause for review**, when a figure or claim can't be traced to the right year's report.

---

## Setup (once)

**1. Install**

```bash
cd azure_based
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Create the Azure resources** (needs the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli))

```bash
az login
bash azure_setup/provision.sh
```

It takes about 10 minutes and ends by printing a block of settings.

**3. Save the settings**

```bash
cp .env.example .env
```

Paste the block that `provision.sh` printed into `.env`.

**4. Put the four PDFs in `azure_based/pdfs/`**

```
pdfs/infosys_fy2025.pdf
pdfs/infosys_fy2026.pdf
pdfs/eternal_fy2025.pdf
pdfs/eternal_fy2026.pdf
```

Or set `LOCAL_PDF_DIR` in `.env` to the folder where they already are.

**5. Upload them, then build the index**

```bash
python ingestion/upload_pdfs.py    # PDFs -> Blob Storage (once)
python run_ingestion.py            # Blob -> PyMuPDF4LLM -> chunks -> AI Search
```

---

## Running it

```bash
python run_query.py                                            # default question
python run_query.py "How did attrition change?"                # your question, Infosys
python run_query.py "How did revenue change?" Eternal          # your question, Eternal
```

Every step also runs on its own, which is how the course teaches it:

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

## Tests

```bash
python -m pytest tests
```

24 tests, all offline: no Azure account, no keys, no cost. They cover the search
filter, the index definition, the search call, the semantic rerank, the
PDF-to-markdown cache, the content-filter handling, the guardrails, and
the review pause.

---

## Cost: read before running

- **AI Search Basic** is billed **per hour** while it exists, even when idle.
- **PDF conversion is free.** PyMuPDF4LLM runs on your machine. Results are cached in
  `data/processed/`, so each report is converted only once.
- **Azure OpenAI** is billed per token. Building the index embeds every chunk once,
  and each question costs a few calls.

Check current prices on the Azure pricing pages before recording. When you're done:

```bash
bash azure_setup/teardown.sh       # deletes everything, stops all charges
```

---

## Folder layout

```
config.py                 every setting (secrets come from .env)
azure_clients.py          one connection per Azure service
run_ingestion.py          build the search index
run_query.py              ask a question, review if needed

ingestion/
  upload_pdfs.py          PDFs -> Blob Storage
  load_pdfs.py            Blob -> PyMuPDF4LLM -> markdown (cached)
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
4. **Semantic ranker.** What it changes (`retrieval/rerank.py` shows each chunk's move).
5. **Guardrails and human review.** Content filter errors, grounding, and the LangGraph pause.

---

## Model versions and regions

`provision.sh` deploys `gpt-4o` (2024-11-20), `gpt-4o-mini` (2024-07-18) and
`text-embedding-3-small` (1) with the `GlobalStandard` SKU in `eastus2`. Azure
retires model versions and quota varies by region. If a deployment fails, check
what your subscription offers in the Azure AI Foundry portal, then change
`LOCATION` or the versions at the top of the script.
