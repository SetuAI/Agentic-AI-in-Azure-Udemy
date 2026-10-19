"""
================================================================================
OUTPUT GUARDRAIL 2 - GROUNDING (is every statement in the sources, in the right year?)
================================================================================

"Is this answer true?" cannot be checked. "Is every statement in this answer
supported by the chunks we retrieved?" can. That narrower question is called
GROUNDING.

TWO CHECKS
----------
    Check 1  NUMBERS   free, instant. Every number in the answer must appear in
                       the retrieved chunks -- and in the RIGHT YEAR's chunks.
    Check 2  CLAIMS    one Azure OpenAI call. A small model reads the answer and
                       the sources and names any statement they do not support.

WHY "THE RIGHT YEAR" MATTERS
----------------------------
The likeliest mistake on this corpus is not an invented number. It is a real
number from FY2025 described as FY2026. A check that only asks "is this number
anywhere in the sources?" accepts that mistake. This one does not.

WHY COMPARISONS ALWAYS GET CHECK 2
----------------------------------
"Margin rose from 20 to 21" and "margin fell from 21 to 20" contain the same
numbers. Only reading the words can tell them apart. So Check 1 may approve an
answer on its own ONLY if the answer claims no change.

WHAT HAPPENS ON FAILURE
-----------------------
Nothing is thrown away. A doubtful answer is ESCALATED to a person (the graph
pauses for review), with the exact problem named so the reviewer can decide fast.

HOW TO RUN
----------
    python guardrails/factual.py       (Check 1 demo is free; Check 2 calls Azure)
================================================================================
"""

import re                                       # regular expressions: find numbers and year labels
import sys                                      # sys.path lets us import from the folder above
from dataclasses import dataclass, field        # @dataclass writes __init__; field() gives each object its own list
from pathlib import Path                        # Path builds file paths
from typing import Dict, List                   # type hints

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable

import config                                   # threshold, deployment name

GROUNDED = "grounded"                           # supported by the sources -- show it
UNGROUNDED = "ungrounded"                       # unusable (e.g. empty) -- do not show it
NEEDS_REVIEW = "needs_review"                   # not sure -- a person decides


@dataclass
class GroundingDecision:
    """The result of checking one answer."""
    verdict: str                                # GROUNDED, UNGROUNDED or NEEDS_REVIEW
    check: str                                  # which check decided: "numbers", "claims" or "input"
    reason: str                                 # a short explanation for the logs
    score: float = 0.0                          # share of numbers that traced back, when measured
    unsupported_items: List[str] = field(default_factory=list)  # exactly what failed -- shown to the reviewer


# ==============================================================================
# SECTION 1 - FINDING NUMBERS AND YEARS IN TEXT
# ==============================================================================

def extract_numbers(text: str) -> List[str]:
    """Every number in the text, with commas removed so '5,000' and '5000' match."""
    raw = re.findall(r"\d{1,3}(?:,\d{2,3})+|\d+\.?\d*", text)  # first try 1,53,670-style, then plain 21.1-style
    return [value.replace(",", "") for value in raw]           # strip the separators


def year_labels(fiscal_year: str) -> List[str]:
    """Patterns that name one fiscal year: FY2025, FY 2025, FY25, 2024-25."""
    end = int(fiscal_year[2:])                  # "FY2025" -> 2025
    short = str(end)[2:]                        # 2025 -> "25"
    return [
        rf"\bFY\s?-?\s?{end}\b",                # FY2025, FY 2025, FY-2025
        rf"\bFY\s?-?\s?{short}\b",              # FY25, FY 25
        rf"\b{end - 1}\s?[-–/]\s?{short}\b",    # 2024-25, 2024–25, 2024/25
    ]


def years_mentioned(sentence: str, fiscal_years: List[str]) -> List[str]:
    """Which fiscal years a sentence names."""
    return [year for year in fiscal_years       # keep each year...
            if any(re.search(p, sentence, re.IGNORECASE) for p in year_labels(year))]  # ...if any label matches


def strip_year_labels(sentence: str, fiscal_years: List[str]) -> str:
    """Remove year labels, so 'FY2026' is not mistaken for the figure 2026."""
    for year in fiscal_years:                   # each year
        for pattern in year_labels(year):       # each way of writing it
            sentence = re.sub(pattern, " ", sentence, flags=re.IGNORECASE)  # replace with a space
    return sentence                             # the sentence without year labels


def split_sentences(text: str) -> List[str]:
    """Split on '. ' or a line break -- never inside 21.1 or 1,53,670."""
    return [p for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]  # drop empty pieces


# ==============================================================================
# SECTION 2 - CHECK 1: NUMBERS, YEAR BY YEAR (free)
# ==============================================================================

def check_numbers(answer: str, sources: Dict[str, List[str]]) -> GroundingDecision:
    """
    Each sentence is checked on its own:
        names one year          -> its numbers must be in THAT year's chunks
        names both, or neither  -> its numbers may be in any chunk
    A heading such as "What FY2026 said:" sets the year for the lines below it.
    """
    years = list(sources)                       # e.g. ["FY2025", "FY2026"]
    numbers_by_year = {                         # every number found in each year's chunks
        year: {n for text in texts for n in extract_numbers(text)}  # a set: instant lookups
        for year, texts in sources.items()
    }
    all_numbers = set().union(*numbers_by_year.values())  # every number from every year

    found, unsupported, current_year = [], [], None  # numbers seen, numbers failed, year set by the last heading
    for sentence in split_sentences(answer):    # one sentence (or bullet line) at a time
        named = years_mentioned(sentence, years)  # which years this sentence names
        if len(named) == 1:                     # exactly one year named...
            current_year = year = named[0]      # ...applies here and to the lines below it
        elif len(named) > 1:                    # both years named (usually the "what changed" summary)...
            current_year = year = None          # ...numbers may come from either year
        else:                                   # no year named...
            year = current_year                 # ...inherit the last heading's year

        allowed = numbers_by_year[year] if year else all_numbers  # where this sentence's numbers must appear
        for number in extract_numbers(strip_year_labels(sentence, years)):  # each figure in the sentence
            found.append(number)                # count it
            if number in allowed:               # traced to the right place?
                continue                        # fine
            if year and number in all_numbers:  # it exists -- but in the OTHER year
                unsupported.append(f"{number} (attributed to {year}, found only in another year)")
            else:                               # it exists nowhere in the sources
                unsupported.append(number)

    if not found:                               # no numbers at all -- this check cannot judge
        return GroundingDecision(NEEDS_REVIEW, "numbers", "answer contains no numbers, this check cannot judge it")

    ratio = (len(found) - len(unsupported)) / len(found)  # share of numbers that traced back
    verdict = GROUNDED if ratio >= config.GROUNDING_THRESHOLD else NEEDS_REVIEW  # compare with the threshold
    return GroundingDecision(verdict, "numbers",
                             f"{len(found) - len(unsupported)} of {len(found)} numbers traced to the right year",
                             ratio, unsupported)


# ==============================================================================
# SECTION 3 - CHECK 2: CLAIMS (one Azure OpenAI call)
# ==============================================================================

GROUNDING_PROMPT = """You check whether an answer is supported by the source text provided.

You are NOT judging whether the answer is true in general. You are judging one
thing only: can every statement in the answer be traced to the sources below?

A statement is UNSUPPORTED if it says something the sources do not say, even if
it sounds plausible or is probably correct.

The sources are grouped by fiscal year. A statement about one year is only
supported by that year's sources -- a figure from FY2025 described as FY2026 is
UNSUPPORTED. A claim that something increased or decreased is only supported if
both years' sources show it, in that direction.

Reply in exactly this format:

VERDICT: SUPPORTED
or
VERDICT: UNSUPPORTED
UNSUPPORTED_CLAIM: <the specific statement not found in the sources>

SOURCES:
{sources}

ANSWER TO CHECK:
{answer}"""


def format_sources(sources: Dict[str, List[str]]) -> str:
    """Lay the sources out under one heading per year, so the checker can see which is which."""
    return "\n\n".join(                         # blank line between years
        f"=== {year} ===\n" + ("\n\n---\n\n".join(texts) or "(no extracts)")  # a heading, then that year's chunks
        for year, texts in sources.items()
    )


def check_claims(answer: str, sources: Dict[str, List[str]]) -> GroundingDecision:
    """Ask the guardrail model whether every statement is supported."""
    from azure_clients import openai_client     # imported here so Check 1 works without Azure settings

    try:
        response = openai_client().chat.completions.create(  # one call to Azure OpenAI
            model=config.GUARDRAIL_DEPLOYMENT,  # the small, cheap deployment -- this is a yes/no job
            temperature=0,                      # same input -> same verdict, every time
            max_tokens=150,                     # the reply is two short lines
            messages=[{"role": "user",          # a single instruction message
                       "content": GROUNDING_PROMPT.format(sources=format_sources(sources), answer=answer)}],
        )
        reply = response.choices[0].message.content.strip()  # the model's reply text

        if "VERDICT: SUPPORTED" in reply.upper():  # the model found nothing unsupported
            return GroundingDecision(GROUNDED, "claims", "every statement traced to the retrieved text", 1.0)

        match = re.search(r"UNSUPPORTED_CLAIM:\s*(.+)", reply, re.IGNORECASE)  # pull out the flagged statement
        claim = match.group(1).strip() if match else "not specified"           # or say it gave none
        return GroundingDecision(NEEDS_REVIEW, "claims", "a statement could not be traced", 0.0, [claim])

    except Exception as error:                  # network error, timeout, Azure content filter, quota...
        # Escalate rather than block: a grounding failure usually means a loose
        # answer, not a harmful one, so a person is the proportionate response.
        return GroundingDecision(NEEDS_REVIEW, "claims", f"grounding check unavailable ({type(error).__name__})")


# ==============================================================================
# SECTION 4 - BOTH CHECKS TOGETHER
# ==============================================================================

COMPARISON_PATTERN = re.compile(                # words that claim a change between years
    r"\b(increas|decreas|ris(e|es|ing)|rose|fell|fall|declin|grew|grow|improv|worsen|deteriorat|"
    r"expand|contract|reduc|higher|lower|up from|down from|compared|versus|vs\.?|change|"
    r"strengthen|weaken|soften)",
    re.IGNORECASE,                              # match "Increased" as well as "increased"
)


def check_grounding(answer: str, sources: Dict[str, List[str]]) -> GroundingDecision:
    """
        numbers fail                    -> claims check too; escalate with both sets of findings
        numbers pass, answer compares   -> claims check decides
        numbers pass, no comparison     -> GROUNDED, no Azure call
    """
    if not answer.strip():                      # empty answer?
        return GroundingDecision(UNGROUNDED, "input", "answer is empty")

    numbers = check_numbers(answer, sources)    # Check 1 -- free

    if numbers.verdict == GROUNDED and not COMPARISON_PATTERN.search(answer):  # all traced, no change claimed
        return numbers                          # approved without spending a model call

    claims = check_claims(answer, sources)      # Check 2 -- one Azure OpenAI call

    if numbers.unsupported_items:               # some numbers did not trace back
        if claims.verdict == GROUNDED:          # even if the model is satisfied...
            return numbers                      # ...a wrong number still needs a person
        claims.unsupported_items = numbers.unsupported_items + claims.unsupported_items  # show everything found
    return claims                               # otherwise the claims check decides


def collect_source_texts(reranked_chunks: Dict[str, List[Dict]]) -> Dict[str, List[str]]:
    """Reduce chunks to their texts, KEEPING the split by year."""
    return {year: [c["text"] for c in chunks] for year, chunks in reranked_chunks.items()}


if __name__ == "__main__":                      # only when run directly
    SOURCES = {                                 # two tiny made-up sources, one per year
        "FY2025": ["Revenue stood at 1,53,670 crore. Operating margin was 21.1 per cent."],
        "FY2026": ["Revenue stood at 1,62,990 crore. Operating margin was 20.8 per cent."],
    }
    for answer in [                             # examples for the free Check 1
        "In FY2025 revenue was 1,53,670 crore.",            # right figure, right year
        "In FY2026 the operating margin was 21.1 per cent.",  # FY2025's figure, labelled FY2026
        "Operating margin was 24.8 per cent.",              # a figure that exists nowhere
    ]:
        d = check_numbers(answer, SOURCES)      # Check 1 only -- no Azure call
        print(f"  {d.verdict.upper():13} {answer}\n                {d.unsupported_items or d.reason}")
