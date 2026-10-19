"""
================================================================================
INPUT GUARDRAIL 2 - TOPIC (is this about the annual reports?)
================================================================================

Two checks, cheapest first:

    1. KEYWORDS     does the question use a finance word ("revenue", "risk")?
                    -> allow straight away, with NO Azure call at all
    2. SIMILARITY   otherwise, embed the question with Azure OpenAI and compare
                    it with example questions we know are on topic
                    -> allow if it is close enough, block if not

Most real questions use a finance word, so most never reach the second check.

HOW TO RUN
----------
    python guardrails/topic.py
================================================================================
"""

import re                                       # regular expressions for whole-word keyword matching
import sys                                      # sys.path lets us import from the folder above
from dataclasses import dataclass               # @dataclass writes __init__ for us
from pathlib import Path                        # Path builds file paths
from typing import List, Optional               # type hints

import numpy as np                              # fast maths on lists of numbers

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # threshold, deployment name, company names
from azure_clients import openai_client         # Azure OpenAI, for embeddings

ALLOW = "allow"                                 # the question may continue
BLOCK = "block"                                 # the question stops here

DOMAIN_KEYWORDS = [                             # any one of these means "clearly about the reports"
    "revenue", "margin", "profit", "loss", "ebitda", "earnings",
    "segment", "guidance", "outlook", "risk", "risks", "headcount",
    "attrition", "employees", "clients", "customers", "orders",
    "growth", "expenses", "capex", "cash flow", "dividend",
    "annual report", "financial year", "fiscal", "quarter",
    "board", "auditor", "governance", "esg", "sustainability",
    "acquisition", "subsidiary", "shareholders", "balance sheet",
]

DOMAIN_REFERENCE_EXAMPLES = [                   # questions we KNOW are on topic; new questions are compared to these
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

REFUSAL_MESSAGE = (                             # what the user sees when the question is off topic
    "I can only answer questions about the annual reports of "
    + " and ".join(config.COMPANIES)            # "Eternal and Infosys"
    + " -- things like revenue, margins, risks, strategy and how those changed "
    "between financial years. I can't give investment advice or help with other topics."
)


@dataclass
class TopicDecision:
    """The result of the topic check."""
    verdict: str                                # ALLOW or BLOCK
    check: str                                  # which check decided: "input", "keyword" or "similarity"
    reason: str                                 # a short explanation for the logs
    score: Optional[float] = None               # the similarity score, when the similarity check ran


def check_by_keywords(question: str) -> Optional[TopicDecision]:
    """ALLOW if a finance keyword appears as a whole word; None means 'cannot tell, ask the next check'."""
    lowered = question.lower()                  # compare in lower case so "Revenue" matches "revenue"
    matched = [k for k in DOMAIN_KEYWORDS        # keep every keyword...
               if re.search(rf"\b{re.escape(k)}\b", lowered)]  # ...found as a whole word ("risk" not in "brisk")
    if matched:                                 # at least one?
        return TopicDecision(ALLOW, "keyword", f"found domain terms: {', '.join(matched)}")
    return None                                 # no keyword -- let the similarity check decide


def embed_texts(texts: List[str]) -> np.ndarray:
    """Embed texts with Azure OpenAI and return them as a numpy table (one row per text)."""
    response = openai_client().embeddings.create(model=config.EMBEDDING_DEPLOYMENT, input=texts)  # one call
    return np.array([item.embedding for item in response.data])  # rows of 1,536 numbers


def cosine_similarity(vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """How alike in direction the vector is to each row of the matrix: 1 = same meaning, 0 = unrelated."""
    vector = vector / (np.linalg.norm(vector) + 1e-10)                          # make its length 1 (1e-10 avoids /0)
    matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)  # make every row's length 1
    return matrix @ vector                      # @ multiplies: one similarity score per row


_reference_embeddings: Optional[np.ndarray] = None  # embeddings of the examples, computed once and remembered


def get_reference_embeddings() -> np.ndarray:
    """Embed the reference examples on first use, then reuse them."""
    global _reference_embeddings                # we are changing the variable defined above, not a local copy
    if _reference_embeddings is None:           # first time?
        _reference_embeddings = embed_texts(DOMAIN_REFERENCE_EXAMPLES)  # one Azure call for all 16 examples
    return _reference_embeddings                # every later call is free


def check_by_similarity(question: str) -> TopicDecision:
    """Compare the question's meaning with the on-topic examples."""
    scores = cosine_similarity(embed_texts([question])[0], get_reference_embeddings())  # one score per example
    best = float(np.max(scores))                # the closest example's score
    closest = DOMAIN_REFERENCE_EXAMPLES[int(np.argmax(scores))]  # which example that was, for the logs
    if best >= config.TOPIC_SIMILARITY_THRESHOLD:  # close enough?
        return TopicDecision(ALLOW, "similarity", f"close in meaning to: '{closest}'", best)
    return TopicDecision(BLOCK, "similarity", "not close to anything in the domain", best)


def check_topic(question: str) -> TopicDecision:
    """Empty -> block. Keyword -> allow (free). Otherwise -> similarity decides (one Azure call)."""
    if not question.strip():                    # nothing but spaces?
        return TopicDecision(BLOCK, "input", "question is empty")
    return check_by_keywords(question) or check_by_similarity(question)  # cheap check first


if __name__ == "__main__":                      # only when run directly
    for q in ["How did revenue change?",        # keyword: allowed with no Azure call
              "What did the company say about its people strategy?",  # no keyword: similarity decides
              "Write me a poem about the sea"]: # off topic: should be blocked
        d = check_topic(q)                      # run the check
        print(f"  {d.verdict.upper():6} via {d.check:10} {q}")  # show the verdict and which check decided
