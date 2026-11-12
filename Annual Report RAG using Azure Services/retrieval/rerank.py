"""
================================================================================
retrieval/rerank.py  -  KEEP THE 5 BEST CHUNKS, BY THE SEMANTIC RANKER'S SCORE
================================================================================

PURPOSE
-------
search.py gives back 20 chunks per year. This file keeps the 5 that best
answer the question, using the score Azure's SEMANTIC RANKER already gave
each one during the search.

    search.py    20 chunks per year, in HYBRID order, each with two scores
    rerank.py    sorts them by the SEMANTIC score, and keeps the best 5   ← THIS FILE

Here, we can notice: this file makes NO call to Azure. The semantic ranker
already ran inside the search call. Its score (0 to 4) is already on every
chunk, under "reranker_score". This file just sorts by it, and cuts.

WHAT PROBLEM DOES IT SOLVE
--------------------------
Hybrid search is fast, but rough. It looks at the question and each chunk
SEPARATELY: words against words, numbers against numbers.

The semantic ranker reads the question and a chunk TOGETHER, and judges how
well that chunk answers it. That's much more accurate. So we trust ITS order
to pick the final 5.

    fast and rough first       hybrid search      whole year → 20
    slow and careful second    semantic ranker    20 → 5

WHY 5, NOT 20
-------------
Every chunk we send to gpt-5.1 is one more thing it has to read, and one more
thing that could mislead it. Fifteen loosely related chunks cost more money,
crowd out the real evidence, and give the model wrong material to build an
answer from. Five good chunks are better.

THE KEY WORDS IN THIS FILE
--------------------------
    reranker_score   the semantic ranker's score, 0 to 4.
                     4 = answers the question completely. 0 = irrelevant.
    score            the hybrid score (around 0.03). Used ONLY as a fallback,
                     if the semantic ranker didn't run.
    FALLBACK         if there are no semantic scores (the ranker is switched
                     off), we print a warning and keep the hybrid order.

WHERE IT FITS
-------------
    pipeline step 2 · retrieve   retrieval/search.py: 20 per year
    pipeline step 3 · rerank     graph/nodes.py calls rerank()       ← THIS FILE
    pipeline step 4 · generate   gpt-5.1 reads the 5 per year

WHAT IT NEEDS IN .env
---------------------
    rerank() needs nothing: it only sorts a list.
    The demo at the bottom calls search.py, so it needs the same settings
    as search.py: AZURE_OPENAI_* and AZURE_SEARCH_*.

HOW TO RUN (from the project folder)
------------------------------------
    python retrieval/rerank.py

EXPECTED OUTPUT
---------------
    FY2025: 20 candidates -> 5 kept

      semantic  hybrid  moved  text
             1       4     +3  <first 60 characters of the chunk>
             2       1     -1  ...
             ... (5 lines)

    FY2026: 20 candidates -> 5 kept
      ...

    "moved" +3 means the semantic ranker moved that chunk UP three places,
    compared with where hybrid search had put it.
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
#   Dict            a dictionary: one chunk, like {"text": "...", "reranker_score": 2.9}
#   List            a list: like the 20 chunks from search.py
#   Optional[int]   "a whole number, or nothing (None)"

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the retrieval/ folder. But config.py is one folder ABOVE
# it. So we add the project folder to the front of sys.path, so "import config"
# below can find it.
#   Path(__file__)   this file
#   .parent          its folder (retrieval/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. We use one setting from it:
#   config.RERANK_TOP_K   5: how many chunks to keep per year


# ── STEP A · KEEP THE BEST 5 ──────────────────────────────────────────────────

def rerank(candidates: List[Dict], top_n: Optional[int] = None) -> List[Dict]:
    # Takes the 20 chunks from search.py (for ONE year). Gives back the best 5,
    # highest semantic score first.
    #   candidates   the 20 chunks, in hybrid order
    #   top_n        how many to keep. Left out → 5, from config.py
    """Keep the top_n candidates by semantic ranker score, best first."""
    # The function's one-line description.

    top_n = top_n or config.RERANK_TOP_K
    # If no top_n was given, use 5 from config.py.
    # "top_n or ..." means: use top_n if it has a value; otherwise use the default.

    if any(c["reranker_score"] is None for c in candidates):
        # First, a safety check: did the semantic ranker actually run?
        #
        # Let's read it from the inside out:
        #   c["reranker_score"] is None    this chunk has NO semantic score
        #   for c in candidates            check that for every chunk
        #   any(...)                       True if even ONE chunk has no score
        #
        # When can that happen? When the semantic ranker is switched off on the
        # search service. Then Azure sends no "@search.reranker_score", and
        # search.py's to_result() stores None for it.
        # Without this check, the sort below would CRASH: Python can't compare
        # None with a number.

        print("  WARNING: no semantic ranker scores -- is the semantic ranker enabled on "
              "your search service? Keeping the hybrid order instead.")
        # So, instead of crashing, we print a clear warning that says what's
        # wrong, and where to look: the search service's semantic ranker setting.
        # (The two pieces of text, side by side inside the brackets, are joined
        # into one line by Python.)

        return candidates[:top_n]
        # And we FALL BACK to the hybrid order: the candidates are already
        # sorted by hybrid score (search.py did that), so we just keep the
        # first 5. The pipeline still works, just with a rougher order.
        # "return" also ends the function here, so the sort below never runs.

    ranked = sorted(
        # The normal path: the semantic ranker did run.
        # sorted(...) builds a NEW list, in the order we ask for. The original
        # "candidates" list stays in hybrid order, untouched. show_comparison()
        # below needs that original order, to see what moved.

        candidates,
        # What to sort: the 20 chunks from search.py.

        key=lambda c: c["reranker_score"],
        # What to sort BY: each chunk's semantic score, 0 to 4.
        # "lambda" is a tiny one-line function: given a chunk c, give back
        # c["reranker_score"].

        reverse=True,
        # Biggest first. So the chunk the ranker scored highest comes first.
    )

    return ranked[:top_n]
    # Keep only the first 5 of the sorted list, and hand them back.
    # [:top_n] means "from the start, up to (not including) position 5".


# ── STEP B · SHOW WHAT THE SEMANTIC RANKER CHANGED ────────────────────────────

def show_comparison(candidates: List[Dict], reranked: List[Dict]) -> None:
    # Prints a small table: for each chunk we kept, its NEW position (semantic),
    # its OLD position (hybrid), and how many places it moved.
    #   candidates   the 20 chunks, in hybrid order (from search.py)
    #   reranked     the 5 we kept, in semantic order (from rerank() above)
    # "-> None": it gives nothing back. It only prints.
    """Print the hybrid order and the semantic order side by side, to SEE what reranking changed."""
    # The function's one-line description.
    # This is for TEACHING and checking. The pipeline itself doesn't need it.

    hybrid_rank = {id(c): i + 1 for i, c in enumerate(candidates)}
    # Build a lookup: for each chunk, its position in the HYBRID list (1 to 20).
    #
    # Let's read it piece by piece:
    #   enumerate(candidates)   goes through the 20 chunks, and gives each one's
    #                           position too: (0, chunk), (1, chunk), ...
    #   i + 1                   positions start at 0 in Python. We add 1, so
    #                           the first chunk is position 1, not 0.
    #   id(c)                   a number Python gives each object, unique while
    #                           it exists. We use it as the chunk's "name".
    #   { ... for ... }         a DICTIONARY COMPREHENSION: builds the whole
    #                           dictionary in one line.
    #
    # Why id(c), and not the chunk's text? Two chunks could have the same text
    # (a repeated footer, for example). But rerank() kept the SAME chunk
    # objects, it didn't copy them. So id(c) finds each kept chunk's exact
    # place in the hybrid list.

    print(f"\n  {'semantic':>8}  {'hybrid':>6}  {'moved':>5}  text")
    # Print the column headings:
    #     semantic  hybrid  moved  text
    #   :>8   means "right-align in 8 characters". Same for :>6 and :>5.
    #         This lines the headings up with the numbers below them.
    #   \n    a blank line first, so the table is easy to see.

    for new_rank, chunk in enumerate(reranked, start=1):
        # Go through the 5 chunks we kept, best first.
        #   new_rank    its position in the SEMANTIC order: 1, 2, 3, 4, 5
        #   start=1     count from 1, not 0

        old_rank = hybrid_rank[id(chunk)]
        # Look up where hybrid search had put this same chunk (1 to 20).

        moved = old_rank - new_rank
        # How many places it moved.
        #   Hybrid had it at 4, semantic puts it at 1:   4 - 1 = +3   moved UP 3
        #   Hybrid had it at 1, semantic puts it at 2:   1 - 2 = -1   moved DOWN 1
        #   Same place in both:                                 0   didn't move
        # A positive number means the semantic ranker PROMOTED the chunk.

        print(f"  {new_rank:>8}  {old_rank:>6}  {moved:>+5}  "
              f"{chunk['text'][:60].replace(chr(10), ' ')}")
        # Print one row of the table:
        #          1       4     +3  Infosys is exposed to currency...
        #
        # The parts:
        #   new_rank:>8        semantic position, right-aligned under "semantic"
        #   old_rank:>6        hybrid position, right-aligned under "hybrid"
        #   moved:>+5          places moved. The "+" makes Python ALWAYS show
        #                      the sign: +3, -1, +0. So up and down are clear.
        #   chunk['text'][:60] the first 60 characters of the chunk
        #   .replace(chr(10), ' ')  chr(10) is the "new line" character. We swap
        #                      it for a space, so each row stays on one line.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python retrieval/rerank.py
    # When the pipeline imports this file, nothing below runs.

    from retrieval.search import search_both_years
    # Bring in search_both_years() from search.py, to get the 20 candidates
    # per year.
    # It's imported HERE, not at the top, because only this demo needs it.
    # rerank() itself never calls Azure: it only sorts what it's given.

    QUESTION = "How did the company describe its currency risk?"
    # The same demo question as search.py, so we can compare the two outputs.

    for year, candidates in search_both_years(QUESTION, "Infosys").items():
        # Search both years for Infosys. Then go through the results one year
        # at a time.
        #   .items()   gives each key and its value together:
        #              ("FY2025", [20 chunks]), then ("FY2026", [20 chunks])

        print(f"\n{year}: {len(candidates)} candidates -> {config.RERANK_TOP_K} kept")
        # Show the cut for this year, e.g. "FY2025: 20 candidates -> 5 kept".

        show_comparison(candidates, rerank(candidates))
        # Two things in one line, read from the inside out:
        #   rerank(candidates)          keep the best 5 by semantic score
        #   show_comparison(..., ...)   print the table: semantic position,
        #                               hybrid position, and how far each moved