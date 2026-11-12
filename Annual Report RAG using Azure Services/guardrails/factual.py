"""
================================================================================
guardrails/factual.py  -  OUTPUT GUARDRAIL 2: GROUNDING (THE FACT CHECK)
================================================================================

PURPOSE
-------
This file checks whether every statement in the ANSWER is backed by the
chunks we retrieved, and backed by the RIGHT YEAR's chunks.

    "In FY2025, revenue was 1,53,670 crore."
        Is 1,53,670 in the FY2025 chunks?   yes → fine
        Is it only in the FY2026 chunks?    → wrong year: flag it
        Is it in no chunk at all?           → made up: flag it

If everything traces back, the answer is GROUNDED, and the user sees it.
If anything doesn't, the answer goes to a PERSON for review, with the exact
problem named.

WHAT PROBLEM DOES IT SOLVE
--------------------------
gpt-5.1 writes fluent, confident answers. But fluent isn't the same as
correct. A model can:
    - make up a number that sounds right
    - take a real number from FY2025, and call it FY2026
    - say "margin rose" when it actually fell

The user can't tell any of this by reading the answer. It all sounds equally
sure. So we check, before the user sees it.

WHAT "GROUNDED" MEANS
---------------------
We can't check "Is this answer TRUE?". We'd need to know everything.

But we CAN check a narrower question:
    "Is every statement in this answer supported by the chunks we retrieved?"

That narrower question is called GROUNDING. An answer is GROUNDED if every
statement in it can be traced back to the chunks it was written from.

THE MISTAKE WE MOST WANT TO CATCH
---------------------------------
On our reports, the most likely mistake is NOT a made-up number. It's a REAL
number from the WRONG YEAR. Remember Video 5D: one chunk held BOTH years'
profit, side by side. It's easy for a model to pick the wrong one.

A check that only asks "Is this number anywhere in the chunks?" would accept
that mistake, because the number IS there, just in the other year. So our
check asks: "Is this number in THIS year's chunks?"

TWO CHECKS
----------
    CHECK 1  NUMBERS   Free and instant. Plain Python, no Azure.
                       Every number in the answer must appear in the chunks,
                       and in the RIGHT YEAR's chunks.

    CHECK 2  CLAIMS    One Azure OpenAI call. A model reads the answer and the
                       chunks together, and names any statement the chunks
                       don't support.

Why do we need Check 2? Because numbers can't tell us DIRECTION.
    "Margin rose from 20 to 21."
    "Margin fell from 21 to 20."
Both have exactly the same numbers. Only reading the WORDS tells them apart.
So any answer that claims a change ("rose", "fell", "increased") always gets
Check 2. Only an answer with no change words can be approved by Check 1 alone.

WHAT HAPPENS WHEN A CHECK FAILS
-------------------------------
Nothing is thrown away. A doubtful answer goes to a PERSON. The graph
pauses, and the reviewer sees the answer, plus the exact numbers or
statements that failed. So they can decide quickly.

THE THREE VERDICTS
------------------
    GROUNDED       everything traces back → show it to the user
    NEEDS_REVIEW   something doesn't, or we can't tell → a person decides
    UNGROUNDED     the answer is unusable, e.g. empty → don't show it

WHERE IT FITS
-------------
    search → rerank → generate (gpt-5.1 writes the answer)
                          │
                          ▼
                      format.py                  (output guardrail 1)
                          │
                          ▼
                      factual.py   ← THIS FILE   (output guardrail 2)
                          │
                          ├── GROUNDED      → the user sees the answer
                          └── NEEDS_REVIEW  → a person reviews it first

    graph/nodes.py → output_guardrail_node() calls check_grounding(), after
    check_format() has passed the answer.

WHAT IT NEEDS IN .env
---------------------
    Check 1 (numbers):   nothing.
    Check 2 (claims):    the AZURE_OPENAI_* settings, and the guardrail
                         deployment (AZURE_OPENAI_GUARDRAIL_DEPLOYMENT,
                         gpt-5.1 in our setup).

HOW TO RUN (from the project folder)
------------------------------------
    python guardrails/factual.py

    The demo runs Check 1 only, on made-up sources. So it makes NO Azure
    call, and works even without a .env.

EXPECTED OUTPUT
---------------
    GROUNDED      In FY2025 revenue was 1,53,670 crore.
                  1 of 1 numbers traced to the right year
    NEEDS_REVIEW  In FY2026 the operating margin was 21.1 per cent.
                  ['21.1 (attributed to FY2026, found only in another year)']
    NEEDS_REVIEW  Operating margin was 24.8 per cent.
                  ['24.8']

CHANGE FROM THE ORIGINAL FILE (for gpt-5.1)
-------------------------------------------
    check_claims() used to send temperature=0 and max_tokens=150.
    gpt-5.1 rejects both, so the call failed every time. Both lines are
    removed. Nothing else in the code has changed.
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import re
# "re" is Python's REGULAR EXPRESSIONS module: it finds patterns in text.
# Here, we use it to find NUMBERS ("1,53,670", "21.1") and YEAR LABELS
# ("FY2025", "2024-25") inside the answer and the chunks.

import sys
# "sys" gives us sys.path: the list of folders Python looks in when we write
# "import something".

from dataclasses import dataclass, field
# "@dataclass" is a shortcut for writing small classes that only hold data.
# We use it for GroundingDecision, below: the result of this check.
# "field" lets each result start with its OWN empty list (see below).

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

from typing import Dict, List
# These are TYPE HINTS. They only DESCRIBE what goes in and what comes out.
# Python doesn't check them.
#   Dict[str, List[str]]   our SOURCES: a dictionary with one list of chunk
#                          texts per year, e.g.
#                          {"FY2025": ["...", "..."], "FY2026": ["...", "..."]}
#   List[str]              a list of texts

sys.path.insert(0, str(Path(__file__).parent.parent))
# This file is inside the guardrails/ folder. But config.py is one folder
# ABOVE it. So we add the project folder to the front of sys.path, so the
# import below can find it.
#   Path(__file__)   this file
#   .parent          its folder (guardrails/)
#   .parent          one folder up (the project folder)

import config
# Our settings file. From it we use:
#   config.GROUNDING_THRESHOLD    0.90: at least 90% of the answer's numbers
#                                 must trace back
#   config.GUARDRAIL_DEPLOYMENT   the model for Check 2 (gpt-5.1 in our setup)
#
# Here, we can notice: we do NOT import azure_clients at the top. Check 2
# imports it inside its own function. So Check 1, and the demo below, work
# even on a laptop with no Azure settings at all.


# ── THE THREE POSSIBLE VERDICTS ───────────────────────────────────────────────

GROUNDED = "grounded"
# Everything traces back to the chunks. Show it to the user.

UNGROUNDED = "ungrounded"
# The answer is unusable, for example it's empty. Don't show it.

NEEDS_REVIEW = "needs_review"
# Something didn't trace back, or we couldn't tell. A person decides.
# graph/build_graph.py looks at this verdict to decide whether to pause.


# ── THE RESULT OF THE CHECK ───────────────────────────────────────────────────

@dataclass
# Turn the class below into a dataclass: Python writes the code that creates it.

class GroundingDecision:
    # One result of the grounding check. It holds five pieces of information.
    """The result of checking one answer."""
    # The class's one-line description.

    verdict: str
    # GROUNDED, UNGROUNDED or NEEDS_REVIEW.

    check: str
    # WHICH check made the decision:
    #   "input"     the answer was empty
    #   "numbers"   Check 1
    #   "claims"    Check 2

    reason: str
    # A short explanation, for the logs.
    # e.g. "3 of 3 numbers traced to the right year"

    score: float = 0.0
    # The share of numbers that traced back, from 0 to 1, when Check 1 measured it.
    #   1.0  = every number traced back
    #   0.5  = half of them did
    # "= 0.0" means: if we don't give a score, it's 0.0.

    unsupported_items: List[str] = field(default_factory=list)
    # EXACTLY what failed: each number or statement that didn't trace back.
    # The reviewer sees this list, so they know where to look.
    #
    # Why field(default_factory=list), and not just "= []"? Because "= []"
    # would make every GroundingDecision SHARE one single list.
    # default_factory=list gives each result its OWN new, empty list.


# ==============================================================================
# SECTION 1 - FINDING NUMBERS AND YEARS IN TEXT
# ==============================================================================
# Before we can check numbers, we need to FIND them. And before we can check
# the YEAR of a number, we need to know which year each sentence talks about.
# These five small helpers do that.

def extract_numbers(text: str) -> List[str]:
    # Takes a text. Gives back every number in it, as a list of texts,
    # with the commas removed.
    #   "Revenue was 1,53,670 crore, a margin of 21.1"  →  ["153670", "21.1"]
    """Every number in the text, with commas removed so '5,000' and '5000' match."""
    # The function's one-line description.

    raw = re.findall(r"\d{1,3}(?:,\d{2,3})+|\d+\.?\d*", text)
    # Find every number. The pattern has two parts, joined by "|" (OR):
    #
    #   \d{1,3}(?:,\d{2,3})+    a number WITH COMMAS, like 1,53,670 or 5,000
    #       \d{1,3}             1 to 3 digits at the start
    #       (?:,\d{2,3})+       then one or more groups of: a comma, then 2 or
    #                           3 digits. "2 or 3" handles BOTH ways of writing
    #                           numbers: Indian (1,53,670) and international
    #                           (153,670).
    #
    #   \d+\.?\d*               a PLAIN number, like 21.1 or 2026
    #       \d+                 one or more digits
    #       \.?                 an optional decimal point
    #       \d*                 any digits after it
    #
    # The comma form is tried FIRST, so "1,53,670" is found as one number,
    # not as "1", "53" and "670".

    return [value.replace(",", "") for value in raw]
    # Remove the commas from each number.
    # Why? Because the answer might write "1,53,670" while the chunk writes
    # "153670". Without the commas, both become "153670", and they match.


def year_labels(fiscal_year: str) -> List[str]:
    # Takes a year like "FY2025". Gives back the PATTERNS for every usual way
    # of writing that year.
    # format.py borrows this function too, so both checks agree on years.
    """Patterns that name one fiscal year: FY2025, FY 2025, FY25, 2024-25."""
    # The function's one-line description.

    end = int(fiscal_year[2:])
    # The year as a number: "FY2025" → 2025.
    #   fiscal_year[2:]   everything from position 2 on: "2025"
    #   int(...)          turn the text "2025" into the number 2025

    short = str(end)[2:]
    # The last two digits: 2025 → "25".

    return [
        # Three patterns for this one year.
        #   \b       a WORD BOUNDARY: the start or end of a word or number
        #   \s?      an optional space
        #   -?       an optional dash

        rf"\bFY\s?-?\s?{end}\b",
        # FY2025, FY 2025, FY-2025

        rf"\bFY\s?-?\s?{short}\b",
        # FY25, FY 25

        rf"\b{end - 1}\s?[-–/]\s?{short}\b",
        # 2024-25, 2024–25, 2024/25
        # Here, we can notice: {end - 1} is 2024. FY2025 runs from April 2024
        # to March 2025, so Indian reports often write it as "2024-25".
        #   [-–/]   a short dash, a long dash, or a slash
    ]


def years_mentioned(sentence: str, fiscal_years: List[str]) -> List[str]:
    # Takes a sentence and our years. Gives back the years the sentence NAMES.
    #   "In FY2025, revenue was..."            →  ["FY2025"]
    #   "From FY2025 to FY2026, it rose..."    →  ["FY2025", "FY2026"]
    #   "Revenue was 1,53,670 crore."          →  []
    """Which fiscal years a sentence names."""
    # The function's one-line description.

    return [year for year in fiscal_years
            if any(re.search(p, sentence, re.IGNORECASE) for p in year_labels(year))]
    # Go through each year, and keep it if ANY of its patterns is found in the
    # sentence.
    #   re.IGNORECASE   also match "fy2025" in small letters


def strip_year_labels(sentence: str, fiscal_years: List[str]) -> str:
    # Takes a sentence. Gives it back with every year label REMOVED.
    #   "In FY2026, revenue was 1,62,990 crore."  →  "In  , revenue was 1,62,990 crore."
    """Remove year labels, so 'FY2026' is not mistaken for the figure 2026."""
    # The function's one-line description.
    #
    # Why? Because extract_numbers() would otherwise find "2026" inside
    # "FY2026", and treat it as a financial figure. Then it would look for
    # "2026" in the chunks, and maybe flag it. "2026" here is a year, not a
    # figure. So we take the year labels out first.

    for year in fiscal_years:
        # Go through each year: "FY2025", then "FY2026".

        for pattern in year_labels(year):
            # Go through each way of writing that year.

            sentence = re.sub(pattern, " ", sentence, flags=re.IGNORECASE)
            # Replace every match with a space.
            #   re.sub(pattern, " ", sentence)   find the pattern, and swap
            #                                    it for " "

    return sentence
    # The sentence, with no year labels left in it.


def split_sentences(text: str) -> List[str]:
    # Takes the answer. Gives back its sentences (and bullet lines), one by one.
    """Split on '. ' or a line break -- never inside 21.1 or 1,53,670."""
    # The function's one-line description.

    return [p for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    # Split the text at two kinds of places:
    #   (?<=[.!?])\s+   a space that comes RIGHT AFTER a . ! or ?
    #                   That's the end of a sentence.
    #                   "21.1" has no space after its dot, so it is NOT split.
    #                   (?<=...) means "just after this", without removing it.
    #   \n+             one or more line breaks: the end of a bullet line
    #
    # "if p.strip()" drops any empty pieces left over from the split.


# ==============================================================================
# SECTION 2 - CHECK 1: NUMBERS, YEAR BY YEAR (free)
# ==============================================================================

def check_numbers(answer: str, sources: Dict[str, List[str]]) -> GroundingDecision:
    # CHECK 1. Takes the answer and the sources (the chunk texts, split by year).
    # Checks that every number in the answer is in the RIGHT YEAR's chunks.
    # No Azure call. Free and instant.
    """
    Each sentence is checked on its own:
        names one year          -> its numbers must be in THAT year's chunks
        names both, or neither  -> its numbers may be in any chunk
    A heading such as "What FY2026 said:" sets the year for the lines below it.
    """
    # The function's description. Let's read the three rules in it:
    #
    #   A sentence names ONE year ("In FY2025, revenue was 1,53,670 crore.")
    #     → its numbers must be in THAT year's chunks.
    #
    #   A sentence names BOTH years ("From FY2025 to FY2026, revenue rose...")
    #     → its numbers may come from either year's chunks.
    #
    #   A sentence names NO year ("Revenue was 1,53,670 crore.")
    #     → it takes the year of the last heading above it.
    #
    # Why the last rule? Because our prompt tells the model to write
    #   "What FY2025 said:"  then some lines,
    #   "What FY2026 said:"  then some lines.
    # The lines under a heading usually don't repeat the year. But they ARE
    # about that heading's year.

    years = list(sources)
    # The years in the sources: ["FY2025", "FY2026"].
    # list(...) of a dictionary gives back its keys.

    numbers_by_year = {
        # For EACH year, collect every number found in that year's chunks.
        # A DICTIONARY COMPREHENSION: a "for" inside { }.

        year: {n for text in texts for n in extract_numbers(text)}
        # For this year: go through each chunk text, and each number in it,
        # and collect them all into a SET.
        # A SET is like a list, but it can answer "is this number in here?"
        # instantly, however big it is. And it keeps each number only once.

        for year, texts in sources.items()
        # Go through the sources one year at a time:
        # ("FY2025", [chunk texts]), then ("FY2026", [chunk texts]).
    }
    # So, for example:
    #   {"FY2025": {"153670", "21.1", ...}, "FY2026": {"162990", "20.8", ...}}

    all_numbers = set().union(*numbers_by_year.values())
    # Every number from BOTH years, together in one set.
    #   numbers_by_year.values()   the two sets: FY2025's, FY2026's
    #   *                          hand them over as separate items
    #   set().union(...)           join them into one set
    # We use this for sentences that name both years, or neither.

    found, unsupported, current_year = [], [], None
    # Three things we keep track of as we go through the sentences:
    #   found          every number we check
    #   unsupported    every number that FAILED, with a short note
    #   current_year   the year set by the last heading (None for now)
    # (Writing them on one line like this sets all three at once.)

    for sentence in split_sentences(answer):
        # Go through the answer, one sentence (or bullet line) at a time.

        named = years_mentioned(sentence, years)
        # Which years does THIS sentence name? [], ["FY2025"], or both.

        if len(named) == 1:
            # It names exactly ONE year.

            current_year = year = named[0]
            # Then that's this sentence's year. And it's also the year for the
            # lines BELOW it, until another year is named.
            # (a = b = value sets both a and b.)

        elif len(named) > 1:
            # It names BOTH years. Usually the "what changed" summary.

            current_year = year = None
            # Then no single year applies. None means: its numbers may come
            # from either year.

        else:
            # It names NO year.

            year = current_year
            # Then use the year of the last heading above it.

        allowed = numbers_by_year[year] if year else all_numbers
        # WHERE this sentence's numbers must appear:
        #   one year known   → only in that year's numbers
        #   no year          → anywhere, in all the numbers

        for number in extract_numbers(strip_year_labels(sentence, years)):
            # Take out the year labels first (so "FY2026" isn't read as the
            # figure 2026), then go through each number left in the sentence.

            found.append(number)
            # Count it: we've checked one more number.

            if number in allowed:
                # Is it where it should be?

                continue
                # Yes. Fine. "continue" skips to the next number.

            if year and number in all_numbers:
                # Not in this year's chunks. But it IS in the OTHER year's.
                # This is the mistake we most want to catch: a real number,
                # described as the wrong year.

                unsupported.append(f"{number} (attributed to {year}, found only in another year)")
                # Record it, with a note that says exactly what went wrong:
                #   "21.1 (attributed to FY2026, found only in another year)"

            else:
                # It's in NO chunk at all, from either year.
                # The model made it up, or calculated it.

                unsupported.append(number)
                # Record just the number.

    if not found:
        # The answer had NO numbers at all.

        return GroundingDecision(NEEDS_REVIEW, "numbers", "answer contains no numbers, this check cannot judge it")
        # Then Check 1 can't judge it. NEEDS_REVIEW here means "I can't tell".
        # check_grounding() below then sends it to Check 2, which reads the
        # words instead.

    ratio = (len(found) - len(unsupported)) / len(found)
    # The SHARE of numbers that traced back.
    #   4 numbers checked, 0 failed  →  (4 - 0) / 4  =  1.0
    #   4 numbers checked, 1 failed  →  (4 - 1) / 4  =  0.75

    verdict = GROUNDED if ratio >= config.GROUNDING_THRESHOLD else NEEDS_REVIEW
    # Compare it with the threshold, 0.90 from config.py:
    #   0.90 or more   → GROUNDED
    #   less           → NEEDS_REVIEW
    # Here, we can notice: with fewer than 10 numbers, a single failure puts
    # the share below 0.90. So in practice, ONE wrong number is enough to send
    # an answer to review.

    return GroundingDecision(verdict, "numbers",
                             f"{len(found) - len(unsupported)} of {len(found)} numbers traced to the right year",
                             ratio, unsupported)
    # Give back the decision:
    #   the verdict, which check decided ("numbers"), a reason like
    #   "3 of 4 numbers traced to the right year", the share, and the list of
    #   numbers that failed.


# ==============================================================================
# SECTION 3 - CHECK 2: CLAIMS (one Azure OpenAI call)
# ==============================================================================
# Check 1 can only check NUMBERS. It can't tell "rose" from "fell".
# So Check 2 gives the answer and the chunks to a model, and asks it to read
# them together.

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
# The instructions we send to the checking model.
# The triple quotes """ ... """ let one text run across many lines.
#
# Let's read what it tells the model:
#   1. Don't judge if the answer is TRUE. Only judge if it's in the SOURCES.
#      (That's grounding.) So the model must not use its own knowledge.
#   2. "Sounds right" isn't enough. If the sources don't say it, it's
#      UNSUPPORTED.
#   3. The year matters: a FY2025 figure called FY2026 is UNSUPPORTED.
#   4. A claim that something went UP or DOWN needs both years to show it,
#      in that direction. This is the part Check 1 can't do.
#   5. Reply in an EXACT format: "VERDICT: SUPPORTED", or "VERDICT:
#      UNSUPPORTED" plus the statement. A fixed format means our code can
#      read the reply reliably.
#
# {sources} and {answer} are blanks. check_claims() fills them in, below.


def format_sources(sources: Dict[str, List[str]]) -> str:
    # Takes the sources (split by year). Gives back ONE text, with a heading
    # for each year, so the checking model can see which chunk is which year:
    #   === FY2025 ===
    #   chunk 1
    #   ---
    #   chunk 2
    #
    #   === FY2026 ===
    #   ...
    """Lay the sources out under one heading per year, so the checker can see which is which."""
    # The function's one-line description.

    return "\n\n".join(
        # Join the years' blocks together, with a blank line between them.

        f"=== {year} ===\n" + ("\n\n---\n\n".join(texts) or "(no extracts)")
        # For one year: a heading like "=== FY2025 ===", then that year's
        # chunks, with a "---" line between each one.
        # "or "(no extracts)"": if a year has no chunks, join gives "" (empty),
        # so write "(no extracts)" instead. Then the model knows that year had
        # nothing, rather than seeing a blank.

        for year, texts in sources.items()
        # Go through the years one by one.
    )


def check_claims(answer: str, sources: Dict[str, List[str]]) -> GroundingDecision:
    # CHECK 2. Takes the answer and the sources. Asks a model whether every
    # statement in the answer is supported by the sources.
    # Costs ONE Azure OpenAI call.
    """Ask the guardrail model whether every statement is supported."""
    # The function's one-line description.

    from azure_clients import openai_client
    # A ready-made connection to Azure OpenAI.
    # It's imported HERE, inside the function, so that Check 1 and the demo
    # work without any Azure settings. Only Check 2 needs Azure.

    try:
        # "try" means: run the code below, and if anything goes wrong, jump to
        # the "except" part further down, instead of crashing.

        response = openai_client().chat.completions.create(
            # ONE call to Azure OpenAI: send the instructions, get a reply.
            #
            # CHANGE FOR gpt-5.1: this call used to also send
            #   temperature=0       (to make the reply the same every time)
            #   max_tokens=150      (to keep the reply short)
            # gpt-5.1 rejects both settings, so they're removed. The fixed
            # reply format in our prompt already keeps the reply short.

            model=config.GUARDRAIL_DEPLOYMENT,
            # Which deployment: the guardrail deployment from .env (gpt-5.1 in
            # our setup). It's a separate setting from the answer-writing
            # model, so we COULD use a smaller, cheaper model for this yes/no
            # job.

            messages=[{"role": "user",
                       "content": GROUNDING_PROMPT.format(sources=format_sources(sources), answer=answer)}],
            # The message we send: ONE user message, holding our full prompt.
            #   GROUNDING_PROMPT.format(...)   fill in the two blanks:
            #     sources=format_sources(sources)   the chunks, under year headings
            #     answer=answer                     the answer to check
        )

        reply = response.choices[0].message.content.strip()
        # The model's reply, as text.
        #   response.choices[0]   the first (and only) reply
        #   .message.content      its text
        #   .strip()              remove spaces at the start and end

        if "VERDICT: SUPPORTED" in reply.upper():
            # Did the model say SUPPORTED?
            #   .upper()   turn the reply into capitals first, so "Verdict:
            #              Supported" counts too.
            #
            # Here, we can notice: "VERDICT: SUPPORTED" is NOT inside
            # "VERDICT: UNSUPPORTED". After the colon and space, one says
            # "SUPPORTED" and the other says "UNSUPPORTED". So this check
            # can't mix them up.

            return GroundingDecision(GROUNDED, "claims", "every statement traced to the retrieved text", 1.0)
            # Yes: every statement is supported. GROUNDED.

        match = re.search(r"UNSUPPORTED_CLAIM:\s*(.+)", reply, re.IGNORECASE)
        # The model said UNSUPPORTED. Now find WHICH statement it named.
        #   UNSUPPORTED_CLAIM:   the label from our reply format
        #   \s*                  any spaces after it
        #   (.+)                 the rest of the line: the statement itself.
        #                        The brackets "capture" it, so we can read it.

        claim = match.group(1).strip() if match else "not specified"
        # If we found the label: take the captured statement (group 1).
        # If the model forgot the label: say "not specified".

        return GroundingDecision(NEEDS_REVIEW, "claims", "a statement could not be traced", 0.0, [claim])
        # NEEDS_REVIEW, with the unsupported statement in the list, so the
        # reviewer sees exactly what the model flagged.

    except Exception as error:
        # Something went wrong with the call: no network, a timeout, Azure's
        # content filter, the quota ran out...

        # Escalate rather than block: a grounding failure usually means a loose
        # answer, not a harmful one, so a person is the proportionate response.
        return GroundingDecision(NEEDS_REVIEW, "claims", f"grounding check unavailable ({type(error).__name__})")
        # We couldn't check. So we DON'T show the answer as if it were checked.
        # And we don't throw it away either. We send it to a person.
        #   type(error).__name__   the error's type, like "APITimeoutError",
        #                          so the logs show what went wrong


# ==============================================================================
# SECTION 4 - BOTH CHECKS TOGETHER
# ==============================================================================

COMPARISON_PATTERN = re.compile(
    # Words that CLAIM A CHANGE between the two years.
    # If an answer uses any of them, Check 1 isn't enough: the numbers can't
    # tell "rose" from "fell". So Check 2 must read it.

    r"\b(increas|decreas|ris(e|es|ing)|rose|fell|fall|declin|grew|grow|improv|worsen|deteriorat|"
    # Up and down words. Some are only the START of a word: "increas" matches
    # "increase", "increased" and "increasing".
    #   ris(e|es|ing)   "rise", "rises" or "rising"

    r"expand|contract|reduc|higher|lower|up from|down from|compared|versus|vs\.?|change|"
    # More change words: "expanded", "reduced", "higher", "up from",
    # "compared with", "vs", "changed", ...

    r"strengthen|weaken|soften)",
    # And a few more: "strengthened", "weakened", "softened".

    re.IGNORECASE,
    # Ignore capital letters, so "Increased" matches as well as "increased".
)


def check_grounding(answer: str, sources: Dict[str, List[str]]) -> GroundingDecision:
    # The main function of this file. graph/nodes.py calls this one.
    # Takes the answer and the sources. Runs Check 1, and Check 2 when needed.
    # ALWAYS gives back one final decision.
    """
        numbers fail                    -> claims check too; escalate with both sets of findings
        numbers pass, answer compares   -> claims check decides
        numbers pass, no comparison     -> GROUNDED, no Azure call
    """
    # The function's description. Let's read the three paths in it:
    #
    #   Check 1 PASSES, and the answer claims NO change
    #     → GROUNDED. Done. No Azure call at all.
    #
    #   Check 1 PASSES, but the answer claims a change ("rose", "fell")
    #     → Check 2 decides, because only reading the words can check the
    #       direction.
    #
    #   Check 1 FAILS (some numbers didn't trace back)
    #     → Check 2 runs too, and the reviewer sees what BOTH checks found.

    if not answer.strip():
        # Is the answer empty, or only spaces?

        return GroundingDecision(UNGROUNDED, "input", "answer is empty")
        # Then it's unusable. UNGROUNDED.

    numbers = check_numbers(answer, sources)
    # Check 1: the numbers, year by year. Free.

    if numbers.verdict == GROUNDED and not COMPARISON_PATTERN.search(answer):
        # Did every number trace back, AND does the answer claim NO change?

        return numbers
        # Then we're done: GROUNDED, without spending an Azure call.

    claims = check_claims(answer, sources)
    # Otherwise, Check 2: a model reads the answer and the chunks together.
    # One Azure OpenAI call.

    if numbers.unsupported_items:
        # Did any numbers FAIL Check 1?

        if claims.verdict == GROUNDED:
            # And yet the model says everything is supported?

            return numbers
            # We still trust Check 1. A number that isn't in the chunks is a
            # fact we can CHECK, not a judgement. So the answer goes to review,
            # with the failed numbers listed.

        claims.unsupported_items = numbers.unsupported_items + claims.unsupported_items
        # Both checks found problems. So put them ALL in one list: the failed
        # numbers first, then the model's flagged statement. The reviewer then
        # sees everything at once.

    return claims
    # Otherwise, Check 2's decision is the final one.


def collect_source_texts(reranked_chunks: Dict[str, List[Dict]]) -> Dict[str, List[str]]:
    # Takes the 5 + 5 reranked chunks (full dictionaries, with scores and
    # labels). Gives back just their TEXTS, still split by year:
    #   {"FY2025": ["text", ...], "FY2026": ["text", ...]}
    # graph/nodes.py calls this, to prepare the sources for check_grounding().
    """Reduce chunks to their texts, KEEPING the split by year."""
    # The function's one-line description.
    #
    # KEEPING the split by year is the important part. Without it, Check 1
    # couldn't tell which year a number came from.

    return {year: [c["text"] for c in chunks] for year, chunks in reranked_chunks.items()}
    # For each year: the year stays the key, and its list of chunks becomes a
    # list of just their "text" values.
    # (A dictionary comprehension, with a list comprehension inside it.)


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python guardrails/factual.py
    # When the pipeline imports this file, nothing below runs.

    SOURCES = {
        # Two tiny MADE-UP sources, one per year. They stand in for the real
        # chunks, so the demo needs no Azure call.

        "FY2025": ["Revenue stood at 1,53,670 crore. Operating margin was 21.1 per cent."],
        # FY2025: revenue 1,53,670, margin 21.1

        "FY2026": ["Revenue stood at 1,62,990 crore. Operating margin was 20.8 per cent."],
        # FY2026: revenue 1,62,990, margin 20.8
    }

    for answer in [
        # Three demo answers, each showing one result of Check 1.

        "In FY2025 revenue was 1,53,670 crore.",
        # The right figure, in the right year. Should be GROUNDED.

        "In FY2026 the operating margin was 21.1 per cent.",
        # 21.1 is FY2025's figure, called FY2026. The mistake we most want to
        # catch. Should be NEEDS_REVIEW, with a "found only in another year" note.

        "Operating margin was 24.8 per cent.",
        # 24.8 is in neither year. A made-up number. Should be NEEDS_REVIEW.
    ]:
        d = check_numbers(answer, SOURCES)
        # Run Check 1 ONLY. No Azure call.

        print(f"  {d.verdict.upper():13} {answer}\n                {d.unsupported_items or d.reason}")
        # Print the verdict and the answer, then on the next line, either the
        # list of failed numbers, or (if nothing failed) the reason.
        #   :13   pad the verdict to 13 characters, so the answers line up
        #   d.unsupported_items or d.reason
        #         an empty list counts as "nothing", so for a GROUNDED answer
        #         we print the reason instead: "1 of 1 numbers traced..."