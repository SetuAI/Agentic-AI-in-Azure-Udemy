"""
================================================================================
RETRIEVAL STEP 2 - RERANK WITH AZURE'S SEMANTIC RANKER
================================================================================

WHAT THIS FILE DOES
-------------------
Cuts each year's 20 candidates down to the 5 that best answer the question,
using the score Azure's SEMANTIC RANKER already gave each one during search.

WHY A SECOND RANKING AT ALL
---------------------------
Hybrid search is fast but shallow. It compares the question and each chunk
separately -- by shared words, and by embeddings made apart from each other.

The semantic ranker is a language model that reads the question and a chunk
TOGETHER, as one input, and judges how well the chunk answers it. That is much
more accurate, and much too slow to run over every chunk in the index -- so
Azure runs it only on the top results of the hybrid search.

    fast and rough first (hybrid search: whole index -> 20)
    slow and careful second (semantic ranker: 20 -> 5)

The OpenAI/Chroma project does the second stage with a cross-encoder running on
your own computer. Here Azure does it as part of the search call -- nothing to
download, nothing to run locally.

WHY 5, NOT 20
-------------
Every chunk sent to the model is something it must read and might be misled by.
Fifteen loosely related chunks cost money, crowd out the real evidence, and give
the model plausible material to reach a wrong conclusion from.

HOW TO RUN
----------
    python retrieval/rerank.py
================================================================================
"""

import sys                                      # sys.path lets us import from the folder above
from pathlib import Path                        # Path builds file paths
from typing import Dict, List, Optional         # type hints

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # how many chunks to keep


def rerank(candidates: List[Dict], top_n: Optional[int] = None) -> List[Dict]:
    """Keep the top_n candidates by semantic ranker score, best first."""
    top_n = top_n or config.RERANK_TOP_K        # default: keep 5

    if any(c["reranker_score"] is None for c in candidates):  # no semantic score? (ranker disabled on the service)
        print("  WARNING: no semantic ranker scores -- is the semantic ranker enabled on "
              "your search service? Keeping the hybrid order instead.")  # explain, rather than fail silently
        return candidates[:top_n]               # fall back to the best hybrid results

    ranked = sorted(                            # a new list, highest semantic score first
        candidates,                             # the 20 candidates from search.py
        key=lambda c: c["reranker_score"],      # sort by the semantic ranker's score
        reverse=True,                           # biggest first
    )
    return ranked[:top_n]                       # keep only the best few


def show_comparison(candidates: List[Dict], reranked: List[Dict]) -> None:
    """Print the hybrid order and the semantic order side by side, to SEE what reranking changed."""
    hybrid_rank = {id(c): i + 1 for i, c in enumerate(candidates)}  # each chunk's position in the hybrid list

    print(f"\n  {'semantic':>8}  {'hybrid':>6}  {'moved':>5}  text")  # column headings
    for new_rank, chunk in enumerate(reranked, start=1):           # the chunks we kept, in their new order
        old_rank = hybrid_rank[id(chunk)]                          # where hybrid search had put it
        moved = old_rank - new_rank                                # positive = the ranker promoted it
        print(f"  {new_rank:>8}  {old_rank:>6}  {moved:>+5}  "      # +5 means it jumped five places
              f"{chunk['text'][:60].replace(chr(10), ' ')}")       # first 60 characters, newlines removed


if __name__ == "__main__":                      # only when run directly
    from retrieval.search import search_both_years  # imported here: only needed for this demo

    QUESTION = "How did the company describe its currency risk?"  # same demo question as search.py
    for year, candidates in search_both_years(QUESTION, "Infosys").items():  # each year separately
        print(f"\n{year}: {len(candidates)} candidates -> {config.RERANK_TOP_K} kept")  # the cut
        show_comparison(candidates, rerank(candidates))           # what moved, and by how much
