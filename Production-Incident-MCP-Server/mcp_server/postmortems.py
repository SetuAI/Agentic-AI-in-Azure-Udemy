"""
mcp_server/postmortems.py
=========================

WHAT THIS FILE IS FOR
---------------------
Searches our past POSTMORTEMS in Azure AI Search. A postmortem is the short
report a team writes after an incident: what happened, why, what fixed it,
and what to do differently next time. The 8 made-up postmortems live in the
postmortems/ folder, and postmortems/build_index.py puts them into Azure AI
Search once, at setup.

WHAT PROBLEM IT SOLVES
----------------------
The log tools answer "what broke, and why?". But an on-call engineer at
2 a.m. also wants to know: "has this happened before, and what fixed it
last time?" That answer is not in the logs. It's in old postmortems, which
nobody remembers. So the AI can now search them.

HOW THE SEARCH WORKS
--------------------
    1. KEYWORD SEARCH finds postmortems that use the question's words.
    2. The SEMANTIC RANKER reads the question and each postmortem together,
       and puts the best match first (a score from 0 to 4).
    No embeddings and no Azure OpenAI are needed, so the project still has
    no model cost on Azure. With only 8 short postmortems, keyword search
    plus the semantic ranker finds the right one.

SIGNING IN
----------
No key. DefaultAzureCredential signs in, the same way as the other tools:
the Entra ID app on your laptop, the web app's managed identity on Azure.
The managed identity needs the "Search Index Data Reader" role: it can
search the index, but never change it.

HOW TO RUN
----------
You never run this file directly. tools.py calls search() from the
search_postmortems tool.

EXPECTED OUTPUT
---------------
search() returns a list of up to 3 postmortems, best match first.
"""

# lru_cache: remembers a function's result, so we connect to Azure AI Search only once.
from functools import lru_cache

# DefaultAzureCredential: signs in to Azure (the Entra ID app on your laptop, the managed identity on Azure).
from azure.identity import DefaultAzureCredential

# SearchClient: Azure's helper for searching one index.
from azure.search.documents import SearchClient

# QueryType: says which kind of search to run. We use SEMANTIC (keyword search + the semantic ranker).
from azure.search.documents.models import QueryType

# Our settings (the search service's address and the index name).
from config import settings

# ---------------------------------------------------------------------------
# Fixed names, shared with postmortems/build_index.py
# ---------------------------------------------------------------------------

# The name of the semantic ranker's settings inside the index.
SEMANTIC_CONFIG = "postmortems-semantic"

# The fields we hand back to the AI for each postmortem.
RETURN_FIELDS = ["id", "title", "date", "services", "severity", "summary", "root_cause", "fix", "lessons"]

# How many postmortems to return: the best 3 are plenty.
TOP = 3


# ---------------------------------------------------------------------------
# Connecting and searching
# ---------------------------------------------------------------------------

# Connects to the index once. @lru_cache makes later calls reuse the same connection.
@lru_cache
def get_client() -> SearchClient:
    """Return one shared connection to the postmortems index."""
    # The search service's address. Stop with a clear message if it is missing from .env.
    endpoint = settings.require("AI_SEARCH_ENDPOINT", settings.AI_SEARCH_ENDPOINT)
    # Connect to our index, signed in with Entra ID (no key).
    return SearchClient(endpoint, settings.POSTMORTEM_INDEX, DefaultAzureCredential())


# Searches the postmortems. The inputs must already be checked by tools.py.
def search(query: str, service: str | None = None) -> list[dict]:
    """Return up to TOP postmortems that best match the query, best first."""
    # Run the search:
    results = get_client().search(
        # The words to look for.
        search_text=query,
        # Keyword search first, then the semantic ranker re-orders the matches.
        query_type=QueryType.SEMANTIC,
        # Which semantic settings to use (created by build_index.py).
        semantic_configuration_name=SEMANTIC_CONFIG,
        # Only postmortems that involve this app, if one was given.
        # services/any(s: s eq 'payments') means "the services list contains payments".
        filter=f"services/any(s: s eq '{service}')" if service else None,
        # Only these fields come back.
        select=RETURN_FIELDS,
        # Only the best few.
        top=TOP,
    )
    # Turn each result into a plain dictionary, with the semantic ranker's score added.
    return [{**{field: result.get(field) for field in RETURN_FIELDS},
             "relevance": round(result.get("@search.reranker_score") or 0, 2)}
            for result in results]