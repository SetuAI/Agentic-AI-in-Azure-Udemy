"""
================================================================================
RETRIEVAL STEP 1 - HYBRID SEARCH IN AZURE AI SEARCH, ONE YEAR AT A TIME
================================================================================

WHAT THIS FILE DOES
-------------------
Finds the 20 chunks most likely to answer the question, inside ONE company and
ONE fiscal year, and does it with one call to Azure AI Search.

THAT ONE CALL DOES THREE THINGS
-------------------------------
    1. KEYWORD search   (BM25)  finds chunks containing the question's words
    2. VECTOR search            finds chunks whose MEANING is close to the question
    3. SEMANTIC ranker          Azure's reranking model reads the question and each
                                candidate TOGETHER and gives it a new score

Keyword + vector together is called HYBRID search. Azure merges the two lists
with a method called Reciprocal Rank Fusion: a chunk ranked high in either list
ends up high in the merged list.

The semantic ranker's scores come back on the same results. We keep both
scores, so the next step (rerank.py) can show what the semantic ranker changed.

WHY THE YEAR FILTER IS STILL ESSENTIAL ON AZURE
-----------------------------------------------
The FY2025 and FY2026 reports use almost the same wording. Neither keyword nor
vector search can tell "revenue was X" in one year from "revenue was Y" in the
other -- both look like an excellent match. Hybrid search does not fix that.

The fix is a FILTER: search inside FY2025 only, then inside FY2026 only. Azure
writes filters in a language called OData:

    company eq 'Infosys' and fiscal_year eq 'FY2025'

HOW TO RUN
----------
    python retrieval/search.py
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths
from typing import Dict, List, Optional         # type hints

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # top-k, deployment names, semantic config name
from azure_clients import openai_client, search_client  # Azure OpenAI for the embedding, Search for the query


def embed_question(question: str) -> List[float]:
    """Turn the question into an embedding -- with the SAME deployment used for the chunks."""
    response = openai_client().embeddings.create(  # one call to Azure OpenAI
        model=config.EMBEDDING_DEPLOYMENT,      # must match ingestion, or the numbers are not comparable
        input=[question],                       # a list with one text in it
        dimensions=config.EMBEDDING_DIMENSIONS, # same length as the indexed vectors
    )
    return response.data[0].embedding           # the EMBEDDING_DIMENSIONS numbers for the question


def odata_quote(value: str) -> str:
    """Wrap a value in single quotes for OData, doubling any quote inside it (O'Brien -> 'O''Brien')."""
    return "'" + value.replace("'", "''") + "'"  # OData's escaping rule: a quote inside text is written twice


def build_filter(company: Optional[str] = None, fiscal_year: Optional[str] = None) -> Optional[str]:
    """Build an OData filter such as: company eq 'Infosys' and fiscal_year eq 'FY2025'."""
    conditions = []                             # each condition is one "field eq 'value'" piece
    if company:                                 # a company was given?
        conditions.append(f"company eq {odata_quote(company)}")         # keep only that company's chunks
    if fiscal_year:                             # a year was given?
        conditions.append(f"fiscal_year eq {odata_quote(fiscal_year)}") # keep only that year's chunks
    return " and ".join(conditions) or None     # join with "and"; no conditions means no filter at all


def to_result(hit: Dict) -> Dict:
    """Turn one Azure search hit into the plain dictionary the rest of the pipeline uses."""
    return {
        "text": hit["text"],                    # the chunk's content
        "company": hit["company"],              # its company
        "fiscal_year": hit["fiscal_year"],      # its year
        "year_label": hit["year_label"],        # the report's own year label
        "position": hit["position"],            # where in the report it came from
        "score": hit["@search.score"],          # the HYBRID score (keyword + vector, merged)
        "reranker_score": hit.get("@search.reranker_score"),  # the SEMANTIC ranker's score, 0 to 4; None if unavailable
    }


def search(question: str, company: Optional[str] = None, fiscal_year: Optional[str] = None,
           top_k: Optional[int] = None) -> List[Dict]:
    """Hybrid search plus semantic ranking. Returns candidates in HYBRID order (before reranking)."""
    from azure.search.documents.models import VectorizedQuery  # describes the vector half of the search

    top_k = top_k or config.VECTOR_SEARCH_TOP_K  # read the config now, not when the function was defined

    hits = search_client().search(              # ONE call to Azure AI Search
        search_text=question,                   # the keyword half: BM25 over the "text" field
        vector_queries=[VectorizedQuery(        # the vector half
            vector=embed_question(question),    # the question's embedding
            k_nearest_neighbors=top_k,          # how many nearest chunks the vector search contributes
            fields="embedding",                 # which field holds the vectors
        )],
        filter=build_filter(company, fiscal_year),  # stay inside one company and one year
        query_type="semantic",                  # also run the semantic ranker over the merged results
        semantic_configuration_name=config.SEMANTIC_CONFIG_NAME,  # the ranker setup stored in the index
        top=top_k,                              # return this many results in total
        select=["text", "company", "fiscal_year", "year_label", "position"],  # fields to return (not the vector)
    )

    results = [to_result(hit) for hit in hits]  # convert every hit to our plain shape

    # Azure returns results in the SEMANTIC ranker's order. We put them back into
    # the hybrid order, so this step shows what search alone found, and the next
    # step (rerank.py) can show what the semantic ranker changed.
    results.sort(key=lambda r: r["score"], reverse=True)  # highest hybrid score first
    return results                              # up to top_k candidates


def search_both_years(question: str, company: str) -> Dict[str, List[Dict]]:
    """Search each fiscal year separately, so both sides of the comparison get evidence."""
    return {                                    # e.g. {"FY2025": [...], "FY2026": [...]}
        year: search(question, company=company, fiscal_year=year)  # one filtered search per year
        for year in config.FISCAL_YEARS         # FY2025, then FY2026
    }


if __name__ == "__main__":                      # only when run directly
    QUESTION = "How did the company describe its currency risk?"  # a typical comparison topic
    COMPANY = "Infosys"                         # one of the two companies

    print(f"filter example:  {build_filter(COMPANY, 'FY2025')}")  # show what OData looks like
    for year, results in search_both_years(QUESTION, COMPANY).items():  # search each year
        print(f"\n{year}: {len(results)} candidates")                   # how many came back
        for r in results[:5]:                                           # show the top five
            print(f"  hybrid {r['score']:.4f}  semantic {r['reranker_score'] or 0:.2f}  "
                  f"{r['text'][:70].replace(chr(10), ' ')}")            # chr(10) is a newline
