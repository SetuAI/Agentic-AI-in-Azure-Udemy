"""
================================================================================
guardrails/topic.py  -  INPUT GUARDRAIL 2: TOPIC
================================================================================

PURPOSE
-------
This file checks whether the user's QUESTION is about our annual reports.
If it is, the question is ALLOWED, and moves on to search. If it isn't, the
question is BLOCKED, and the user sees a polite message saying what this app
can answer.

    "How did revenue change?"           → ALLOW   (about the reports)
    "Write me a poem about the sea"     → BLOCK   (nothing to do with them)

WHAT PROBLEM DOES IT SOLVE
--------------------------
Our app answers ONE kind of question: what Infosys's and Eternal's annual
reports say, and how that changed between FY2025 and FY2026.

Without this check, an off-topic question would still go through the whole
pipeline:
    - a search in Azure AI Search, twice (once per year)
    - a long call to gpt-5.1, to write an answer
    - another call to check the answer
That's money and time spent on a question we can't answer. Worse, gpt-5.1
might answer it anyway from its general knowledge, which isn't from our
reports at all.

So we stop off-topic questions here, near the start.

HOW DOES IT CHECK
-----------------
Two checks, CHEAPEST FIRST:

    CHECK 1  KEYWORDS     Does the question use a finance word, like
                          "revenue" or "risk"?
                          → yes: ALLOW straight away. No Azure call. Free.
                          → no:  we can't tell yet. Go to Check 2.

    CHECK 2  SIMILARITY   Turn the question into 512 numbers (an EMBEDDING),
                          and compare it with 16 example questions that we
                          KNOW are on topic.
                          → close enough to one of them: ALLOW
                          → not close to any of them:    BLOCK
                          This costs one small Azure OpenAI call.

Most real questions use a finance word. So most of them never need Check 2.

Why do we need Check 2 at all? Because a good question doesn't always use a
finance word. "What did the company say about its people strategy?" has none
of our keywords. But its MEANING is close to "What was said about attrition
and hiring?". Check 2 catches that.

THE KEY WORDS IN THIS FILE
--------------------------
    EMBEDDING           a list of 512 numbers that describes the MEANING of
                        a text. Texts with similar meanings get similar numbers.
                        (The same idea as the chunks in our index.)
    COSINE SIMILARITY   one number that says how similar two embeddings are.
                        Close to 1 = very similar meaning. Close to 0 = unrelated.
    THRESHOLD           the line between ALLOW and BLOCK: 0.40, set in
                        config.py as TOPIC_SIMILARITY_THRESHOLD.

WHERE IT FITS
-------------
    question
       │
       ▼
    safety.py                 (input guardrail 1)
       │
       ▼
    topic.py    ← THIS FILE   (input guardrail 2)
       │
       ▼
    search → rerank → generate → format.py → factual.py

    graph/nodes.py → input_guardrail_node() calls check_topic(), after
    check_safety() has allowed the question.

WHAT IT NEEDS IN .env
---------------------
    Check 1 (keywords):     nothing.
    Check 2 (similarity):   the AZURE_OPENAI_* settings, and the embedding
                            deployment, text-embedding-3-small.

HOW TO RUN (from the project folder)
------------------------------------
    python guardrails/topic.py

    The first demo question needs no Azure call. The other two need Check 2,
    so this makes THREE small embedding calls in total: one for the 16
    examples (made once, then remembered), and one for each of those two
    questions. So your .env must be set.

EXPECTED OUTPUT
---------------
    ALLOW  via keyword    How did revenue change?
    ALLOW  via similarity What did the company say about its people strategy?
    BLOCK  via similarity Write me a poem about the sea
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import re
# "re" is Python's REGULAR EXPRESSIONS module: it finds patterns in text.
# We use it to find a keyword as a WHOLE word, so "risk" isn't found inside
# "brisk".

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from dataclasses import dataclass
# "@dataclass" is a shortcut for writing small classes that only hold data.
# We use it for TopicDecision, below: the result of this check.

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import List, Optional
# These are TYPE HINTS. They only DESCRIBE what goes in and what comes out.
# Python doesn't check them.
#   List              a list, like our list of keywords
#   Optional[...]     "this thing, or nothing (None)"

import numpy as np
# NumPy: a library for fast maths on lists of numbers.
# We use it to compare embeddings: 512 numbers each.
# "as np" just gives it a short name, so we can write np.max(...).

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the guardrails/ folder. But config.py and
# azure_clients.py are one folder ABOVE it. So we add the project folder to
# the front of sys.path, so the imports below can find them.
#   Path(__file__)   this file
#   .parent          its folder (guardrails/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. From it we use:
#   config.EMBEDDING_DEPLOYMENT         "text-embedding-3-small"
#   config.EMBEDDING_DIMENSIONS         512
#   config.TOPIC_SIMILARITY_THRESHOLD   0.40: the line between ALLOW and BLOCK
#   config.COMPANIES                    ["Eternal", "Infosys"]: for the refusal message

from azure_clients import openai_client
# A ready-made connection to Azure OpenAI, from azure_clients.py.
# Check 2 uses it to turn questions into embeddings.


# ── THE TWO POSSIBLE VERDICTS ─────────────────────────────────────────────────

ALLOW = "allow"
# The question is on topic. It may continue to search.

BLOCK = "block"
# The question is off topic. It stops here.


# ── CHECK 1 · THE KEYWORDS ────────────────────────────────────────────────────

DOMAIN_KEYWORDS = [
    # Words that only come up in questions about a company's reports.
    # If a question uses ANY one of them, we allow it straight away.
    # ("Domain" just means "our subject area": company annual reports.)

    "revenue", "margin", "profit", "loss", "ebitda", "earnings",
    # money the company made, and what it kept

    "segment", "guidance", "outlook", "risk", "risks", "headcount",
    # business units, plans for next year, risks, and staff numbers

    "attrition", "employees", "clients", "customers", "orders",
    # people leaving, staff, and customers

    "growth", "expenses", "capex", "cash flow", "dividend",
    # growth, spending, and money paid to shareholders.
    # "capex" is capital expenditure: money spent on things like offices
    # and equipment.

    "annual report", "financial year", "fiscal", "quarter",
    # words about the reports and their time periods

    "board", "auditor", "governance", "esg", "sustainability",
    # how the company is run and checked.
    # "esg" is Environmental, Social and Governance.

    "acquisition", "subsidiary", "shareholders", "balance sheet",
    # companies bought or owned, owners, and the main financial statement
]


# ── CHECK 2 · THE EXAMPLE QUESTIONS ───────────────────────────────────────────

DOMAIN_REFERENCE_EXAMPLES = [
    # 16 questions we KNOW are on topic.
    # In Check 2, a new question is compared with each of these. If its
    # meaning is close enough to ANY one of them, it's on topic.
    # So, here, we can notice: the wider this list covers our subject, the
    # fewer good questions get blocked by mistake.

    "How did revenue change between the two financial years?",
    "What did the company say about operating margins?",
    "What are the main risks disclosed in the annual report?",
    "How did the company describe its currency exposure?",
    "What changed in the risk factors compared with last year?",
    "How many employees does the company have?",
    "What was said about attrition and hiring?",
    "How did the segment performance differ across the years?",
    "What does management say about the outlook for next year?",
    "How did the company describe its AI strategy?",
    "What were the largest expenses during the year?",
    "How did the auditor's report describe the accounts?",
    "What acquisitions were completed during the year?",
    "What did the board report on governance?",
    "How did client concentration change year on year?",
    "What did the company disclose about its dividend?",
]


# ── WHAT THE USER SEES WHEN A QUESTION IS BLOCKED ─────────────────────────────

REFUSAL_MESSAGE = (
    # The message for an off-topic question. graph/nodes.py imports it.
    # The brackets let us build one text across several lines.

    "I can only answer questions about the annual reports of "
    # the first part of the message

    + " and ".join(config.COMPANIES)
    # Join the company names with " and " between them:
    #   ["Eternal", "Infosys"]  →  "Eternal and Infosys"
    # Reading the names from config.py means the message stays correct if
    # we ever add a company.

    + " -- things like revenue, margins, risks, strategy and how those changed "
    "between financial years. I can't give investment advice or help with other topics."
    # The rest of the message: what we CAN answer, and what we can't.
    # So the user knows how to ask a question that will work.
)


# ── THE RESULT OF THE CHECK ───────────────────────────────────────────────────

@dataclass
# Turn the class below into a dataclass: Python writes the code that creates it.

class TopicDecision:
    # One result of the topic check. It holds four pieces of information.
    """The result of the topic check."""
    # The class's one-line description.

    verdict: str
    # ALLOW or BLOCK.

    check: str
    # WHICH check made the decision:
    #   "input"        the question was empty
    #   "keyword"      Check 1 found a finance word
    #   "similarity"   Check 2 compared meanings

    reason: str
    # A short explanation, for the logs.
    # e.g. "found domain terms: revenue"

    score: Optional[float] = None
    # The similarity score, from 0 to 1, but ONLY when Check 2 ran.
    # "= None" means: if we don't give a score, it's None. Check 1 doesn't
    # measure similarity, so it leaves this out.


# ── CHECK 1 · FIND A KEYWORD ──────────────────────────────────────────────────

def check_by_keywords(question: str) -> Optional[TopicDecision]:
    # Takes the question. Gives back either:
    #   a TopicDecision with ALLOW, if a finance keyword is found, or
    #   None, which means "I can't tell; let Check 2 decide"
    #
    # Here, we can notice: Check 1 can ALLOW, but it never BLOCKS. A question
    # without our keywords may still be on topic. Only Check 2 can block.
    """ALLOW if a finance keyword appears as a whole word; None means 'cannot tell, ask the next check'."""
    # The function's one-line description.

    lowered = question.lower()
    # Turn the question into small letters, so "Revenue" matches "revenue".

    matched = [k for k in DOMAIN_KEYWORDS
               if re.search(rf"\b{re.escape(k)}\b", lowered)]
    # Make a list of every keyword found in the question.
    # (A LIST COMPREHENSION: a short "for" and "if" inside [ ].)
    #
    # Let's read the pattern from the inside out:
    #   re.escape(k)    if a keyword has a special character, treat it as
    #                   plain text. Our keywords are plain words, so this just
    #                   keeps the pattern safe.
    #   \b ... \b       a WORD BOUNDARY on each side. So the keyword must be a
    #                   WHOLE word: "risk" is NOT found inside "brisk".
    #                   (That's also why the list has both "risk" and
    #                   "risks": each one only matches as a whole word.)
    #   rf"..."         r = take backslashes literally (\b stays \b),
    #                   f = fill in the {...} part with the keyword

    if matched:
        # Did we find at least one keyword?
        # (An empty list counts as "nothing" in Python, so this is False if
        # nothing matched.)

        return TopicDecision(ALLOW, "keyword", f"found domain terms: {', '.join(matched)}")
        # Yes: ALLOW. And the reason lists the words we found, e.g.
        # "found domain terms: revenue".

    return None
    # No keyword found. We can't tell yet, so Check 2 decides.


# ── CHECK 2 · COMPARE MEANINGS ────────────────────────────────────────────────

def embed_texts(texts: List[str]) -> np.ndarray:
    # Takes a list of texts. Gives back their embeddings, as a TABLE of numbers:
    # one ROW per text, 512 numbers in each row.
    # (np.ndarray is NumPy's name for a table of numbers.)
    """Embed texts with Azure OpenAI and return them as a numpy table (one row per text)."""
    # The function's one-line description.

    response = openai_client().embeddings.create(model=config.EMBEDDING_DEPLOYMENT, input=texts,
                                           dimensions=config.EMBEDDING_DIMENSIONS)
    # ONE call to Azure OpenAI, for all the texts in the list together.
    #   model        text-embedding-3-small: the same model as our chunks
    #   input        the texts to turn into numbers
    #   dimensions   512 numbers per text

    return np.array([item.embedding for item in response.data])
    # The reply holds one result per text. From each, take its .embedding
    # (512 numbers), and stack them into one NumPy table.
    # So for 16 texts, we get a table of 16 rows × 512 numbers.


def cosine_similarity(vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    # Takes ONE embedding (the question) and a TABLE of embeddings (the 16
    # examples). Gives back 16 scores: how similar the question is to each one.
    """How alike in direction the vector is to each row of the matrix: 1 = same meaning, 0 = unrelated."""
    # The function's one-line description.
    #
    # COSINE SIMILARITY compares the DIRECTION of two lists of numbers, not
    # their size. For embeddings:
    #   close to 1   the two texts mean nearly the same thing
    #   close to 0   the two texts are unrelated
    #
    # It's done in two steps: make every list the same length (1), then
    # multiply them. The two lines below do the first step; the last line
    # does the second.

    vector = vector / (np.linalg.norm(vector) + 1e-10)
    # Make the question's embedding have a length of exactly 1.
    #   np.linalg.norm(vector)   the list's current length
    #   vector / length          divide every number by it
    #   + 1e-10                  add a tiny number (0.0000000001), so we
    #                            never divide by zero

    matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)
    # Do the same for EVERY row of the table: each of the 16 examples.
    #   axis=1           measure the length of each ROW, separately
    #   keepdims=True    keep the answer in a shape that lines up with the
    #                    rows, so each row is divided by its own length

    return matrix @ vector
    # "@" multiplies the table by the question's list.
    # Now that everything has length 1, this gives exactly the cosine
    # similarity: one score per row. So 16 scores, one for each example.


_reference_embeddings: Optional[np.ndarray] = None
# A place to REMEMBER the embeddings of the 16 examples.
# It starts as None: we haven't made them yet.
# The "_" at the start of the name is a habit that says "used only inside
# this file".


def get_reference_embeddings() -> np.ndarray:
    # Gives back the embeddings of the 16 examples.
    # The first time, it asks Azure for them. Every time after, it reuses them.
    """Embed the reference examples on first use, then reuse them."""
    # The function's one-line description.
    #
    # Why remember them? The 16 examples never change. Asking Azure for the
    # same 16 embeddings for every question would cost time and money for
    # nothing.

    global _reference_embeddings
    # We're about to CHANGE the variable _reference_embeddings defined above.
    # "global" tells Python to change that one, not make a new copy inside
    # this function.

    if _reference_embeddings is None:
        # Is this the first time? (Nothing remembered yet?)

        _reference_embeddings = embed_texts(DOMAIN_REFERENCE_EXAMPLES)
        # Then embed all 16 examples, in ONE Azure call, and remember them.

    return _reference_embeddings
    # Give them back. From the second question on, this is free.


def check_by_similarity(question: str) -> TopicDecision:
    # Takes the question. ALWAYS gives back a decision: ALLOW or BLOCK.
    # This is the only check in this file that can BLOCK a real question.
    """Compare the question's meaning with the on-topic examples."""
    # The function's one-line description.

    scores = cosine_similarity(embed_texts([question])[0], get_reference_embeddings())
    # Score the question against each of the 16 examples.
    # Let's read it from the inside out:
    #   embed_texts([question])      the question's embedding, as a table of
    #                                ONE row (one Azure call)
    #   [0]                          take that one row: 512 numbers
    #   get_reference_embeddings()   the 16 examples' embeddings
    #   cosine_similarity(...)       16 scores, one per example

    best = float(np.max(scores))
    # The HIGHEST of the 16 scores: how close the question is to the example
    # it's MOST like.
    # Why the highest, not the average? Because a question only needs to be
    # like ONE kind of on-topic question. "How many employees?" is close to
    # the employees example, and far from the dividend example. That's fine.
    # float(...) turns NumPy's number into a plain Python number.

    closest = DOMAIN_REFERENCE_EXAMPLES[int(np.argmax(scores))]
    # WHICH example was the closest, for the logs.
    #   np.argmax(scores)   the POSITION of the highest score (0 to 15)
    #   int(...)            turn it into a plain Python number
    #   DOMAIN_REFERENCE_EXAMPLES[...]   the example at that position

    if best >= config.TOPIC_SIMILARITY_THRESHOLD:
        # Is the best score at least 0.40?

        return TopicDecision(ALLOW, "similarity", f"close in meaning to: '{closest}'", best)
        # Yes: close enough to one of our examples. ALLOW.
        # And we keep the score and the closest example, for the logs.

    return TopicDecision(BLOCK, "similarity", "not close to anything in the domain", best)
    # No: not close to ANY of our 16 examples. BLOCK.


# ── BOTH CHECKS TOGETHER ──────────────────────────────────────────────────────

def check_topic(question: str) -> TopicDecision:
    # The main function of this file. graph/nodes.py calls this one.
    # Takes the question. ALWAYS gives back a decision: ALLOW or BLOCK.
    """Empty -> block. Keyword -> allow (free). Otherwise -> similarity decides (one Azure call)."""
    # The function's one-line description.

    if not question.strip():
        # First, is the question EMPTY, or only spaces?
        #   .strip()   removes spaces from both ends. If nothing is left,
        #              the question was empty.

        return TopicDecision(BLOCK, "input", "question is empty")
        # Then BLOCK. There's nothing to search for.

    return check_by_keywords(question) or check_by_similarity(question)
    # Cheapest check first.
    #   check_by_keywords(...)   ALLOW if a keyword is found, otherwise None
    #   or                       if that gave None, run the next one...
    #   check_by_similarity(...) ...which compares meanings, and decides.
    #
    # So, here, we can notice: if Check 1 finds a keyword, Check 2 NEVER runs,
    # and there's no Azure call at all.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python guardrails/topic.py
    # When the pipeline imports this file, nothing below runs.

    for q in ["How did revenue change?",
              "What did the company say about its people strategy?",
              "Write me a poem about the sea"]:
        # Three demo questions, one for each path through this file:
        #   1. "How did revenue change?"
        #        has the keyword "revenue" → ALLOW by Check 1, no Azure call
        #   2. "...its people strategy?"
        #        no keyword, but close in meaning to our examples
        #        → ALLOW by Check 2
        #   3. "Write me a poem about the sea"
        #        no keyword, and not close to anything → BLOCK by Check 2

        d = check_topic(q)
        # Run the topic check on this question.

        print(f"  {d.verdict.upper():6} via {d.check:10} {q}")
        # Print the verdict, which check decided, and the question:
        #   ALLOW  via keyword    How did revenue change?
        #
        #   .upper()   "allow" → "ALLOW"
        #   :6, :10    pad to 6 and 10 characters, so the columns line up