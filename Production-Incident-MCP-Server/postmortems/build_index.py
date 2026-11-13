"""
postmortems/build_index.py
==========================

WHAT THIS FILE IS FOR
---------------------
Puts the 8 postmortems in this folder into Azure AI Search, so the
search_postmortems tool can find them. You run it ONCE, at setup (and again
whenever you add or change a postmortem).

WHAT IT DOES, STEP BY STEP
--------------------------
    1. Reads every PM-*.md file in this folder, and splits each one into
       fields: id, title, date, services, severity, and its 5 sections.
    2. Creates the index "postmortems" in Azure AI Search (or updates it,
       if it already exists), with a SEMANTIC configuration, so the
       semantic ranker can put the best match first.
    3. Uploads the 8 postmortems into it.

THE FORMAT OF A POSTMORTEM FILE
-------------------------------
    # PM-2026-03 · Payments ran out of database connections ...   <- id · title
    - Date: 2026-03-14                                             <- details
    - Services: payments, api-gateway
    - Severity: SEV-1
    ## Summary              ## Impact             ## Root cause    <- sections
    ## What fixed it        ## Lessons and follow-ups

WHY THERE ARE NO EMBEDDINGS
---------------------------
We search with keywords plus the semantic ranker only. That needs no Azure
OpenAI, so the project keeps zero model cost on Azure. For 8 short reports,
it finds the right one.

SIGNING IN
----------
No key. DefaultAzureCredential signs in as the Entra ID app in .env, which
needs two roles on the search service: "Search Service Contributor" (to
create the index) and "Search Index Data Contributor" (to upload). See
docs/azure/ai-search.md.

HOW TO RUN
----------
From the project folder:

    uv run python -m postmortems.build_index

EXPECTED OUTPUT
---------------
    Read 8 postmortems from postmortems/
    Index 'postmortems' is ready
    Uploaded 8 of 8 postmortems
"""

# re: finds patterns in text (the "- Key: value" lines and the "## Section" headings).
import re

# Path: a simple way to work with folders and file names.
from pathlib import Path

# DefaultAzureCredential: signs in to Azure as the Entra ID app in .env.
from azure.identity import DefaultAzureCredential

# SearchClient: uploads documents into one index.
from azure.search.documents import SearchClient

# SearchIndexClient: creates and updates indexes.
from azure.search.documents.indexes import SearchIndexClient

# The building blocks of an index: its fields, and its semantic settings.
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
)

# Our settings (the search service's address and the index name).
from config import settings

# Our logger, to print progress messages with the time.
from config.logging_config import get_logger

# The semantic settings' name, shared with the search tool so both always match.
from mcp_server.postmortems import SEMANTIC_CONFIG

# A logger named after this file.
logger = get_logger(__name__)

# The folder this file is in: the postmortem files sit next to it.
FOLDER = Path(__file__).parent

# Each "## heading" in a file, and the field it goes into.
SECTIONS = {
    "Summary": "summary",
    "Impact": "impact",
    "Root cause": "root_cause",
    "What fixed it": "fix",
    "Lessons and follow-ups": "lessons",
}


# ---------------------------------------------------------------------------
# Step 1: read the files
# ---------------------------------------------------------------------------

# Splits one postmortem file into the index's fields.
def parse_postmortem(text: str) -> dict:
    """Return one postmortem as a dictionary of index fields."""
    # The first line is "# PM-2026-03 · Title". Split it at the " · ".
    first_line = text.splitlines()[0].removeprefix("# ")
    # The part before " · " is the id, the part after is the title.
    pm_id, title = first_line.split(" · ", 1)
    # Every "- Key: value" line, as {"Date": "2026-03-14", ...}.
    details = dict(re.findall(r"^- (\w+): (.+)$", text, flags=re.MULTILINE))
    # Split the text at each "## " heading. parts = [before, heading1, body1, heading2, body2, ...].
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    # Pair each heading with its body, as {"Summary": "...", "Root cause": "...", ...}.
    bodies = {heading.strip(): body.strip() for heading, body in zip(parts[1::2], parts[2::2])}
    # Build the document. Every field name matches a field in the index.
    return {
        # The key. Azure AI Search keys may hold letters, numbers, "-" and "_": "PM-2026-03" is fine.
        "id": pm_id.strip(),
        # The title, searchable.
        "title": title.strip(),
        # The date, as text like 2026-03-14 (sorts correctly as text).
        "date": details["Date"],
        # The services, as a list: "payments, api-gateway" -> ["payments", "api-gateway"].
        "services": [name.strip() for name in details["Services"].split(",")],
        # How serious it was.
        "severity": details["Severity"],
        # The 5 sections, each under its field name.
        **{field: bodies[heading] for heading, field in SECTIONS.items()},
    }


# Reads every postmortem file in this folder.
def read_postmortems() -> list[dict]:
    """Return every PM-*.md file in this folder, parsed, oldest id first."""
    # Read and parse each file, in name order.
    return [parse_postmortem(path.read_text(encoding="utf-8")) for path in sorted(FOLDER.glob("PM-*.md"))]


# ---------------------------------------------------------------------------
# Step 2: the index
# ---------------------------------------------------------------------------

# Describes the index: its fields, and its semantic settings.
def make_index() -> SearchIndex:
    """Return the description of the postmortems index."""
    # The fields, one per piece of a postmortem:
    #   SimpleField      -> stored and filterable, but not searched by words
    #   SearchableField  -> searched by words (keyword search)
    fields = [
        # The key: every document's unique id.
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        # The title: searched by words.
        SearchableField(name="title", type=SearchFieldDataType.String),
        # The date: filterable and sortable, so the newest can come first if needed.
        SimpleField(name="date", type=SearchFieldDataType.String, filterable=True, sortable=True),
        # The services: a LIST of words, filterable, so the tool can ask "only payments".
        SearchableField(name="services", collection=True, type=SearchFieldDataType.String, filterable=True),
        # The severity: filterable.
        SimpleField(name="severity", type=SearchFieldDataType.String, filterable=True),
        # The 5 sections: all searched by words.
        *[SearchableField(name=field, type=SearchFieldDataType.String) for field in SECTIONS.values()],
    ]
    # The semantic settings: which fields the semantic ranker reads, most important first.
    semantic = SemanticSearch(configurations=[SemanticConfiguration(
        # The name the search tool asks for.
        name=SEMANTIC_CONFIG,
        # The fields, in order of importance.
        prioritized_fields=SemanticPrioritizedFields(
            # The title says what the postmortem is about.
            title_field=SemanticField(field_name="title"),
            # The ranker reads these to judge the match: the cause matters most, then the fix.
            content_fields=[SemanticField(field_name=field) for field in ("root_cause", "fix", "summary", "lessons")],
            # The services are short keywords.
            keywords_fields=[SemanticField(field_name="services")],
        ),
    )])
    # The whole index: its name, its fields and its semantic settings.
    return SearchIndex(name=settings.POSTMORTEM_INDEX, fields=fields, semantic_search=semantic)


# ---------------------------------------------------------------------------
# Step 3: run everything
# ---------------------------------------------------------------------------

# Builds the index and uploads the postmortems.
def main() -> None:
    """Read the postmortems, create or update the index, and upload them."""
    # The search service's address. Stop with a clear message if it is missing from .env.
    endpoint = settings.require("AI_SEARCH_ENDPOINT", settings.AI_SEARCH_ENDPOINT)
    # Sign in to Azure as the Entra ID app in .env.
    credential = DefaultAzureCredential()
    # Step 1: read the files.
    documents = read_postmortems()
    # Say how many we found.
    logger.info("Read %d postmortems from %s/", len(documents), FOLDER.name)
    # Step 2: create the index, or update it if it already exists.
    SearchIndexClient(endpoint, credential).create_or_update_index(make_index())
    # Say it is ready.
    logger.info("Index '%s' is ready", settings.POSTMORTEM_INDEX)
    # Step 3: upload the documents. merge_or_upload: add new ones, and replace ones with the same id.
    results = SearchClient(endpoint, settings.POSTMORTEM_INDEX, credential).merge_or_upload_documents(documents)
    # Count how many were accepted.
    uploaded = sum(1 for result in results if result.succeeded)
    # Say how many made it.
    logger.info("Uploaded %d of %d postmortems", uploaded, len(documents))


# Run main() only when this file is started directly (not when it is imported).
if __name__ == "__main__":
    # Build the index.
    main()