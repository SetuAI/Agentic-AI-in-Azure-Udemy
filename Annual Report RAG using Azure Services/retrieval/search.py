"""
================================================================================
retrieval/search.py  -  HYBRID SEARCH IN AZURE AI SEARCH, ONE YEAR AT A TIME
================================================================================

PURPOSE
-------
This file finds the chunks most likely to answer the question, inside ONE
company and ONE fiscal year. It does it with ONE call to Azure AI Search.

That one call does three things:

    1. FILTER           only this company, only this year
    2. HYBRID SEARCH    keyword search + vector search, merged into one list
    3. SEMANTIC RANKER  Azure's reranker reads the question and each chunk
                        TOGETHER, and gives each one a score from 0 to 4

And it does all of this TWICE: once for FY2025, once for FY2026.

WHAT PROBLEM DOES IT SOLVE
--------------------------
Our index holds 5,154 chunks, from 2 companies and 2 years. For a question
about Infosys, we need only the few Infosys chunks that answer it, from each
year SEPARATELY.

In Video 5D, a plain search in the portal brought back the WRONG COMPANY
(Eternal chunks) and the WRONG YEAR (an FY2025 chunk for an FY2026 question).
This file fixes both, with a filter. And it searches by MEANING as well as by
WORDS, which the portal's search box couldn't do.

THE KEY WORDS IN THIS FILE
--------------------------
    KEYWORD SEARCH   finds chunks that contain the question's WORDS.
                     Azure uses a method called BM25: it counts how often the
                     words appear, and gives rare words more weight.
    VECTOR SEARCH    finds chunks whose MEANING is close to the question:
                     it turns the question into 512 numbers, and finds the
                     chunks whose 512 numbers are closest.
    HYBRID SEARCH    both at once, merged into one list.
    RRF              Reciprocal Rank Fusion: HOW Azure merges the two lists.
                     It compares POSITIONS, not scores. A chunk near the top
                     of either list ends up high; near the top of both,
                     very high. That's why the hybrid score is small, e.g. 0.03.
    SEMANTIC RANKER  reads the question and each chunk together, and scores
                     how well the chunk answers it: 0 (irrelevant) to 4
                     (answers it completely). It reads up to 50 of the merged
                     results, and we get back its best 20.
    OData FILTER     the language Azure uses for filters, e.g.
                     company eq 'Infosys' and fiscal_year eq 'FY2025'

WHERE IT FITS
-------------
    pipeline step 2 · retrieve   graph/nodes.py calls search_both_years()  ← THIS FILE
    pipeline step 3 · rerank     retrieval/rerank.py keeps the best 5 of these 20

WHAT IT NEEDS IN .env
---------------------
    AZURE_OPENAI_*  settings     to turn the question into 512 numbers
    AZURE_SEARCH_*  settings     to search the index

HOW TO RUN (from the project folder)
------------------------------------
    python retrieval/search.py

EXPECTED OUTPUT
---------------
    filter example:  company eq 'Infosys' and fiscal_year eq 'FY2025'

    FY2025: 20 candidates
      hybrid 0.0xxx  semantic x.xx  <first 70 characters of the chunk>
      ... (5 lines: the top five)

    FY2026: 20 candidates
      hybrid 0.0xxx  semantic x.xx  ...
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import Dict, List, Optional
# These are TYPE HINTS. They only DESCRIBE what goes into a function and what
# comes out. Python doesn't check them.
#   Dict            a dictionary, like one result: {"text": "...", "score": 0.03}
#   List            a list, like 20 results, or the 512 numbers of an embedding
#   Optional[str]   "text, or nothing (None)". Used for settings that can be left out.

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the retrieval/ folder. But config.py and azure_clients.py
# are one folder ABOVE it. So we add the project folder to the front of
# sys.path, so the imports below can find them.
#   Path(__file__)   this file
#   .parent          its folder (retrieval/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. From it we use:
#   config.EMBEDDING_DEPLOYMENT     "text-embedding-3-small": to embed the question
#   config.EMBEDDING_DIMENSIONS     512: how many numbers per embedding
#   config.VECTOR_SEARCH_TOP_K      20: how many results we ask for, per year
#   config.SEMANTIC_CONFIG_NAME     "annual-reports-semantic": the ranker's settings
#   config.FISCAL_YEARS             ["FY2025", "FY2026"]: the years we search, one by one

from azure_clients import openai_client, search_client
# Two ready-made connections, from azure_clients.py:
#   openai_client()   Azure OpenAI: turns the QUESTION into 512 numbers
#   search_client()   Azure AI Search, DATA side: runs the search on our index


# ── STEP A · TURN THE QUESTION INTO 512 NUMBERS ───────────────────────────────

def embed_question(question: str) -> List[float]:
    # Takes the question, as text. Gives back its embedding: a list of 512 numbers.
    # Vector search needs this: it compares the QUESTION's numbers with each
    # CHUNK's numbers, and finds the closest.
    """Turn the question into an embedding -- with the SAME deployment used for the chunks."""
    # The function's one-line description.

    response = openai_client().embeddings.create(
        # Send the question to Azure OpenAI, and get back its embedding.
        # This is the same call build_index.py made for every chunk.

        model=config.EMBEDDING_DEPLOYMENT,
        # Which model: "text-embedding-3-small".
        #
        # Important: this MUST be the same model that embedded the chunks in
        # build_index.py. Each model turns text into numbers in its own way.
        # So numbers from two different models can't be compared, and the
        # vector search would bring back the wrong chunks.

        input=[question],
        # The text to embed. The call expects a LIST of texts, so we put our
        # one question inside a list: [question].

        dimensions=config.EMBEDDING_DIMENSIONS,
        # How many numbers: 512. The SAME as the chunks in the index.
        # If the question had 1,536 numbers and the chunks 512, Azure couldn't
        # compare them, and the search would fail.
    )

    return response.data[0].embedding
    # The reply holds one result per text we sent. We sent one, so we take the
    # first: response.data[0]. And from it, its .embedding: the 512 numbers.


# ── STEP B · THE FILTER ───────────────────────────────────────────────────────
# A FILTER narrows the search down to the rows we want, BEFORE anything is
# scored. Azure writes filters in a small language called OData, for example:
#     company eq 'Infosys' and fiscal_year eq 'FY2025'
# "eq" means "equals". Text values go inside single quotes.

def odata_quote(value: str) -> str:
    # Takes a value, like Infosys. Gives it back wrapped in single quotes,
    # ready to go inside an OData filter: 'Infosys'.
    """Wrap a value in single quotes for OData, doubling any quote inside it (O'Brien -> 'O''Brien')."""
    # The function's one-line description.

    return "'" + value.replace("'", "''") + "'"
    # Let's read it from the inside out:
    #   value.replace("'", "''")   if the value itself contains a single quote,
    #                              write it twice. That's OData's rule.
    #   "'" + ... + "'"            put one single quote at each end
    #
    # Why does this matter? Imagine a company called O'Brien. Without this,
    # the filter would be  company eq 'O'Brien'  and Azure would think the
    # value ends after the O. With this, it's  company eq 'O''Brien'  which
    # Azure reads correctly.
    # Our two companies have no quotes in their names. But this keeps the
    # filter safe for any name.


def build_filter(company: Optional[str] = None, fiscal_year: Optional[str] = None) -> Optional[str]:
    # Takes a company and a year (either can be left out). Gives back the filter
    # text, e.g. "company eq 'Infosys' and fiscal_year eq 'FY2025'".
    # If both are left out, it gives back None: no filter at all.
    """Build an OData filter such as: company eq 'Infosys' and fiscal_year eq 'FY2025'."""
    # The function's one-line description.

    conditions = []
    # An empty list. Each condition, like "company eq 'Infosys'", is added here.

    if company:
        # Was a company given?

        conditions.append(f"company eq {odata_quote(company)}")
        # Then add a condition: the chunk's company must equal this company.
        # For Infosys:  company eq 'Infosys'

    if fiscal_year:
        # Was a year given?

        conditions.append(f"fiscal_year eq {odata_quote(fiscal_year)}")
        # Then add a condition: the chunk's year must equal this year.
        # For FY2025:  fiscal_year eq 'FY2025'
        #
        # This is the condition that keeps the two years apart. It only works
        # because build_index.py marked fiscal_year as filterable.

    return " and ".join(conditions) or None
    # Join the conditions with " and " between them:
    #   ["company eq 'Infosys'", "fiscal_year eq 'FY2025'"]
    #     →  "company eq 'Infosys' and fiscal_year eq 'FY2025'"
    #
    # "or None": if the list is empty, join gives "" (empty text). Empty text
    # counts as "nothing" in Python, so "or None" gives back None instead.
    # And None tells Azure: no filter at all.


# ── STEP C · ONE RESULT, IN OUR OWN SIMPLE SHAPE ──────────────────────────────

def to_result(hit: Dict) -> Dict:
    # Takes ONE result from Azure (called a HIT). Gives back a plain dictionary,
    # with just the parts the rest of our pipeline needs.
    """Turn one Azure search hit into the plain dictionary the rest of the pipeline uses."""
    # The function's one-line description.

    return {
        # A new dictionary, with seven keys.
        # Why not just pass Azure's hit along? Because it has extra keys we
        # don't need, and Azure's own key names (with "@search."). This gives
        # the rest of the pipeline one simple, fixed shape to work with.

        "text": hit["text"],
        # The chunk's text: our "text" field from the index.

        "company": hit["company"],
        # Its company, e.g. "Infosys".

        "fiscal_year": hit["fiscal_year"],
        # Its year, e.g. "FY2025".

        "year_label": hit["year_label"],
        # How the report writes its year, e.g. "FY 2024-25".

        "position": hit["position"],
        # Where in the report it came from, e.g. 850.

        "score": hit["@search.score"],
        # The HYBRID score: keyword and vector search, merged with RRF.
        # It's small, around 0.03, because it's built from the chunk's
        # POSITIONS in the two lists, not from a word count or a distance.

        "reranker_score": hit.get("@search.reranker_score"),
        # The SEMANTIC RANKER's score: 0 to 4.
        #
        # Why .get(...) and not [...] like the others? Because this score is
        # only there if the semantic ranker ran. If it's switched off on the
        # search service, the key is missing.
        #   hit["..."]       would CRASH if the key is missing
        #   hit.get("...")   gives back None instead, and carries on
        # rerank.py then sees None, and prints a clear warning.
    }


# ── STEP D · THE SEARCH ITSELF ────────────────────────────────────────────────

def search(question: str, company: Optional[str] = None, fiscal_year: Optional[str] = None,
           top_k: Optional[int] = None) -> List[Dict]:
    # The main function of this file. It runs ONE search, for ONE year.
    #   question      the user's question
    #   company       e.g. "Infosys"   (optional)
    #   fiscal_year   e.g. "FY2025"    (optional)
    #   top_k         how many results to return. Left out → 20, from config.py
    # Gives back a list of up to 20 results, each in our simple shape (Step C).
    """Hybrid search plus semantic ranking. Returns candidates in HYBRID order (before reranking)."""
    # The function's one-line description.

    from azure.search.documents.models import VectorizedQuery
    # VectorizedQuery describes the VECTOR half of the search: which numbers to
    # compare, against which field, and how many nearest chunks to bring.
    # "Vectorized" means we've already turned the question into numbers
    # ourselves (Step A), and we're handing Azure those numbers.
    # It's imported here, inside the function, because only this function needs it.

    top_k = top_k or config.VECTOR_SEARCH_TOP_K
    # If no top_k was given, use 20 from config.py.
    # "top_k or ..." means: use top_k if it has a value; otherwise use the default.
    #
    # Why not write "top_k=config.VECTOR_SEARCH_TOP_K" in the function's first
    # line instead? Because Python would read that value ONCE, when the file
    # loads. Reading it here, every time, means a change to config.py (or a test
    # that changes it) always takes effect.

    hits = search_client().search(
        # Now, ONE call to Azure AI Search. Everything below is sent together,
        # and Azure does the filter, the hybrid search and the semantic ranker
        # in one go.

        search_text=question,
        # THE KEYWORD HALF.
        # The question, as text. Azure looks for its words inside the "text"
        # field (the field we made a SearchableField in build_index.py),
        # and scores each chunk with BM25.

        vector_queries=[VectorizedQuery(
            # THE VECTOR HALF.
            # It's a list, because a search can include several vector searches.
            # We use one.

            vector=embed_question(question),
            # The question's 512 numbers (Step A).
            # Azure compares them with every chunk's 512 numbers, using HNSW to
            # do it quickly (set up in build_index.py).

            k_nearest_neighbors=top_k,
            # How many of the closest chunks the vector search adds to the mix: 20.
            # "Nearest neighbours" means the chunks whose numbers are closest
            # to the question's numbers.

            fields="embedding",
            # Which field holds the numbers to compare: our "embedding" field.
        )],

        filter=build_filter(company, fiscal_year),
        # THE FILTER (Step B): e.g. company eq 'Infosys' and fiscal_year eq 'FY2025'.
        # Azure applies it FIRST. So both the keyword half and the vector half
        # only ever see this one company's chunks from this one year.

        query_type="semantic",
        # Switch on the SEMANTIC RANKER.
        # After the two lists are merged, the ranker reads the question together
        # with each of the top merged results (up to 50), and scores each one 0 to 4.

        semantic_configuration_name=config.SEMANTIC_CONFIG_NAME,
        # Which ranker settings to use: "annual-reports-semantic".
        # Created in build_index.py. It tells the ranker to read the "text" field.

        top=top_k,
        # How many results to send back: 20.
        # Here, we can notice: Azure sends them back in the SEMANTIC RANKER's
        # order, best first. So these 20 are the ranker's best 20.

        select=["text", "company", "fiscal_year", "year_label", "position"],
        # Which of our fields to send back with each result.
        # Why not "embedding"? Because we don't need the 512 numbers back. Leaving
        # them out keeps each reply small and fast.
    )

    results = [to_result(hit) for hit in hits]
    # Go through every hit Azure sent back, and turn each one into our simple
    # shape (Step C). Now "results" is a plain list of up to 20 dictionaries.
    # (This short "for" inside [ ] is called a LIST COMPREHENSION.)

    # Azure returns results in the SEMANTIC ranker's order. We put them back into
    # the hybrid order, so this step shows what search alone found, and the next
    # step (rerank.py) can show what the semantic ranker changed.
    results.sort(key=lambda r: r["score"], reverse=True)
    # Now, we SORT these 20 by their HYBRID score, highest first.
    #   key=lambda r: r["score"]   sort by each result's "score" (the hybrid score)
    #                              "lambda" is a tiny one-line function: given
    #                              a result r, give back r["score"]
    #   reverse=True               biggest first
    #
    # Why undo Azure's order? For TEACHING. With the 20 in hybrid order here,
    # rerank.py can sort them by the semantic score, and SHOW us which chunks
    # the semantic ranker moved up or down. The web page shows this too, as the
    # "before" and "after" columns.
    #
    # Note: these 20 were already picked by the semantic ranker, from up to 50
    # merged results. So the "before" order is the hybrid order of the
    # ranker's best 20, not of the very first 20 hybrid results.

    return results
    # Hand back up to 20 results, in hybrid order.


# ── STEP E · BOTH YEARS ───────────────────────────────────────────────────────

def search_both_years(question: str, company: str) -> Dict[str, List[Dict]]:
    # Runs search() twice: once for FY2025, once for FY2026.
    # Gives back a dictionary with one list of results for each year:
    #   {"FY2025": [20 results], "FY2026": [20 results]}
    # This is the function the pipeline calls, in step 2 (graph/nodes.py).
    """Search each fiscal year separately, so both sides of the comparison get evidence."""
    # The function's one-line description.
    #
    # Why search the two years SEPARATELY, instead of once across both?
    # Because then FY2026 chunks could fill most of the 20 places, and FY2025
    # would have almost no evidence. Searching each year on its own guarantees
    # that BOTH years get their own best results, for a fair comparison.

    return {
        # Build the dictionary in one go. This short form is called a
        # DICTIONARY COMPREHENSION: a "for" inside { }.

        year: search(question, company=company, fiscal_year=year)
        # For each year: the key is the year ("FY2025"), and the value is
        # that year's search results.

        for year in config.FISCAL_YEARS
        # Go through the years in config.py: "FY2025", then "FY2026".
    }


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python retrieval/search.py
    # When the pipeline imports this file, nothing below runs.

    QUESTION = "How did the company describe its currency risk?"
    # A demo question. Currency risk is in every annual report, in both years,
    # so it's a good topic to compare.

    COMPANY = "Infosys"
    # One of our two companies.

    print(f"filter example:  {build_filter(COMPANY, 'FY2025')}")
    # First, show what an OData filter looks like:
    #   filter example:  company eq 'Infosys' and fiscal_year eq 'FY2025'

    for year, results in search_both_years(QUESTION, COMPANY).items():
        # Search both years. Then go through the dictionary we get back, one
        # year at a time.
        #   .items()   gives each key and its value together:
        #              ("FY2025", [results]), then ("FY2026", [results])

        print(f"\n{year}: {len(results)} candidates")
        # How many results this year got, e.g. "FY2025: 20 candidates".
        # "\n" first: a blank line between the two years, so it's easy to read.

        for r in results[:5]:
            # Go through the top FIVE results only (results[:5] means the
            # first five), so the output stays short.

            print(f"  hybrid {r['score']:.4f}  semantic {r['reranker_score'] or 0:.2f}  "
                  f"{r['text'][:70].replace(chr(10), ' ')}")
            # Print one line per result, with both scores and the start of the text:
            #   hybrid 0.0331  semantic 2.87  Infosys is exposed to currency...
            #
            # The parts:
            #   r['score']:.4f                hybrid score, 4 digits after the point
            #   r['reranker_score'] or 0      the semantic score, or 0 if it's missing
            #   :.2f                          2 digits after the point
            #   r['text'][:70]                the first 70 characters of the chunk
            #   .replace(chr(10), ' ')        chr(10) is the "new line" character.
            #                                 We swap it for a space, so each
            #                                 result stays on one line.