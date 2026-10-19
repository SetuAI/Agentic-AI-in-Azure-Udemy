"""
================================================================================
AZURE PIPELINE - EVERY SETTING IN ONE FILE
================================================================================

This is the Azure version of the annual-report pipeline. It is a separate
project: it shares no code with the OpenAI/Chroma version, and nothing here
changes how that version runs.

Every Azure service the pipeline talks to is configured here:

    Azure OpenAI                     writes answers, embeds text, runs checks
    Azure AI Search                  stores the chunks, searches, reranks
    Azure Blob Storage               holds the PDFs in the cloud

PDFs are turned into markdown locally with PyMuPDF4LLM (free), not with
Azure Document Intelligence (billed per page -- too costly for these reports).

Secrets (keys, endpoints) are NOT written here. They are read from a .env file
in this folder. Copy .env.example to .env and fill it in -- or run
azure_setup/provision.sh, which prints the values for you.
================================================================================
"""

import os                                   # os.getenv reads environment variables
from pathlib import Path                    # Path builds file paths that work on every OS

from dotenv import load_dotenv              # load_dotenv copies .env values into the environment


# ==============================================================================
# SECTION 1 - FOLDERS
# ==============================================================================

PROJECT_ROOT = Path(__file__).parent                    # this folder: azure_based/

load_dotenv(PROJECT_ROOT / ".env", override=True)       # read azure_based/.env; its values win over the shell's

DATA_DIR = PROJECT_ROOT / "data"                        # everything this pipeline writes lives under here
RAW_PDF_DIR = DATA_DIR / "raw"                          # PDFs downloaded from Blob Storage land here
PROCESSED_DIR = DATA_DIR / "processed"                  # markdown from PyMuPDF4LLM is cached here

# Where upload_pdfs.py finds the PDFs to send to Blob Storage. Put the four
# reports in azure_based/pdfs/, or point LOCAL_PDF_DIR in .env at another folder.
LOCAL_PDF_DIR = Path(os.getenv("LOCAL_PDF_DIR", "pdfs")).expanduser()  # expanduser turns ~ into your home folder
if not LOCAL_PDF_DIR.is_absolute():                     # a relative path like ./pdfs...
    LOCAL_PDF_DIR = PROJECT_ROOT / LOCAL_PDF_DIR        # ...means relative to azure_based/, wherever you run from


# ==============================================================================
# SECTION 2 - THE DOCUMENTS
# ==============================================================================
# The four annual reports. Each entry is the file name (locally and in Blob
# Storage) plus the labels that every chunk of it carries into the search index.
# ==============================================================================

DOCUMENTS = [                                                   # one dictionary per annual report
    {"filename": "infosys_fy2025.pdf",                          # file name, locally and in Blob Storage
     "company": "Infosys",                                      # which company -- used as a search filter
     "fiscal_year": "FY2025",                                   # year ended 31 March 2025 -- used as a search filter
     "year_label": "FY 2024-25"},                               # how the report itself writes the year
    {"filename": "infosys_fy2026.pdf",                          # Infosys, the following year
     "company": "Infosys",                                      # same company
     "fiscal_year": "FY2026",                                   # year ended 31 March 2026
     "year_label": "FY 2025-26"},                               # how the report writes it
    {"filename": "eternal_fy2025.pdf",                          # Eternal (formerly Zomato), first year
     "company": "Eternal",                                      # second company
     "fiscal_year": "FY2025",                                   # year ended 31 March 2025
     "year_label": "FY 2024-25"},                               # how the report writes it
    {"filename": "eternal_fy2026.pdf",                          # Eternal, the following year
     "company": "Eternal",                                      # same company
     "fiscal_year": "FY2026",                                   # year ended 31 March 2026
     "year_label": "FY 2025-26"},                               # how the report writes it
]

COMPANIES = sorted({doc["company"] for doc in DOCUMENTS})         # ["Eternal", "Infosys"] -- a set removes repeats
FISCAL_YEARS = sorted({doc["fiscal_year"] for doc in DOCUMENTS})  # ["FY2025", "FY2026"]


# ==============================================================================
# SECTION 3 - AZURE OPENAI
# ==============================================================================
# On Azure you do not call a model by its name. You first create a DEPLOYMENT of
# the model in your Azure OpenAI resource, give that deployment a name, and then
# call the deployment name. provision.sh names each deployment after its model,
# so the defaults below match what it creates.
# ==============================================================================

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")       # e.g. https://my-openai.openai.azure.com/
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")         # key 1 or key 2 from the resource's "Keys" page
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")  # REST API version the SDK speaks

GENERATION_DEPLOYMENT = os.getenv("AZURE_OPENAI_GENERATION_DEPLOYMENT", "gpt-4o")      # writes the comparison answer
GUARDRAIL_DEPLOYMENT = os.getenv("AZURE_OPENAI_GUARDRAIL_DEPLOYMENT", "gpt-4o-mini")   # cheaper model for yes/no checks
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")  # turns text into vectors

EMBEDDING_DIMENSIONS = 1536                     # length of each vector text-embedding-3-small returns; the index must match


# ==============================================================================
# SECTION 4 - AZURE AI SEARCH
# ==============================================================================

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")       # e.g. https://my-search.search.windows.net
AZURE_SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")         # an ADMIN key: we create the index and upload to it
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX", "annual-reports")  # name of the index we create

SEMANTIC_CONFIG_NAME = "annual-reports-semantic"  # name of the semantic ranker setup stored inside the index

VECTOR_SEARCH_TOP_K = 20                        # candidates fetched per year -- a wide net for the ranker to choose from
RERANK_TOP_K = 5                                # chunks kept per year after the semantic ranker re-scores them


# ==============================================================================
# SECTION 5 - BLOB STORAGE
# ==============================================================================

AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")    # one string: account name + key
AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "annual-reports")  # container (folder) holding the PDFs


# ==============================================================================
# SECTION 6 - CHUNKING AND GUARDRAIL THRESHOLDS
# ==============================================================================
# Same values as the OpenAI/Chroma project, so the two can be compared fairly.
# ==============================================================================

CHUNK_SIZE = 1200                               # characters per chunk, roughly one healthy paragraph
CHUNK_OVERLAP = 200                             # characters repeated between neighbours so no sentence is cut in two
MIN_CHUNK_LENGTH = 100                          # anything shorter (page numbers, stray headers) is thrown away

TOPIC_SIMILARITY_THRESHOLD = 0.40               # how close a question must be to our examples to count as on-topic
GROUNDING_THRESHOLD = 0.90                      # share of an answer's numbers that must trace back to the sources
INTERRUPT_ON_LOW_GROUNDING = True               # False runs straight through without pausing for review


# ==============================================================================
# SECTION 7 - CHECKING THE SETTINGS
# ==============================================================================
# Failing early with a clear list is kinder than a confusing 401 error from deep
# inside an API call. Each Azure client calls require() with only the settings
# it needs, so a lesson that only uses Azure OpenAI works before the other
# services exist.
# ==============================================================================

def require(*names: str) -> None:
    """Stop with a clear message if any of the named settings is empty."""
    missing = [name for name in names if not globals().get(name)]   # look each name up among this file's settings
    if missing:                                                     # anything not filled in?
        raise RuntimeError(                                         # stop, saying exactly what to fix
            "These settings are missing from azure_based/.env:\n  "
            + "\n  ".join(missing)                                  # one missing name per line
            + "\nCopy .env.example to .env, or run azure_setup/provision.sh."
        )
