"""
================================================================================
run_query.py  -  ASK ONE QUESTION: THE WHOLE PIPELINE, WITH HUMAN REVIEW AT THE KEYBOARD
================================================================================

PURPOSE
-------
This file puts ONE question through our whole pipeline, from the terminal,
and prints what happened at every stage:

    1. QUESTION         what was asked, and about which company
    2. BLOCKED          only if a guardrail refused the question
    3. RETRIEVAL        the 5 chunks kept per year, and what the semantic
                        ranker changed
    4. OUTPUT CHECKS    the grounding verdict, and anything that failed
    5. HUMAN REVIEW     only if the answer couldn't be verified: the graph
                        PAUSES, and asks YOU to approve, correct or reject it
    6. ANSWER           the final answer the user would see

WHAT PROBLEM DOES IT SOLVE
--------------------------
We've built the pipeline piece by piece: search, rerank, guardrails, and the
graph that connects them. But we haven't yet run ONE real question through
ALL of it, and watched each step.

This file does exactly that, with no web page in the way. And it plays the
part of the HUMAN REVIEWER right in the terminal. So we can see the pause and
the resume happen, step by step: the same thing the web app does with buttons.

WHERE IT FITS
-------------
    run_ingestion.py    builds the index                (run once, before)
    run_query.py        THIS FILE: ask one question      (from the terminal)
    UI/server.py        the same pipeline, in a web page

    It calls graph/build_graph.py, which runs all 7 nodes from graph/nodes.py.

WHAT IT NEEDS IN .env
---------------------
    The AZURE_OPENAI_* and AZURE_SEARCH_* settings, and a built index.

HOW TO RUN (from the project folder)
------------------------------------
    python run_query.py
        the default question, about Infosys

    python run_query.py "How did revenue change between the two years?"
        your own question, about Infosys

    python run_query.py "How did revenue change between the two years?" Eternal
        your own question, about Eternal

    python run_query.py "Should I buy Infosys shares?"
        a question the safety guardrail refuses

EXPECTED OUTPUT
---------------
    One of three endings:
        BLOCKED BY AN INPUT GUARDRAIL     the question was refused
        ANSWER SHOWN TO THE USER          the answer was grounded: no review
        HUMAN REVIEW REQUIRED             the graph paused; you type a or r,
          then ANSWER SHOWN TO THE USER   and the run carries on
================================================================================
"""


# ── IMPORTS ───────────────────────────────────────────────────────────────────

import sys
# "sys" gives us two things here:
#   sys.argv   the words typed after "python run_query.py" on the command line
#   sys.path   the list of folders Python looks in when we write "import ..."

import uuid
# "uuid" makes a UNIQUE ID, like "3f2a9c1e-...". Each run gets its own one,
# so the checkpointer can tell this run's saved state apart from any other.

from pathlib import Path
# "Path" lets us build file and folder paths that work on every operating system.

sys.path.insert(0, str(Path(__file__).parent))
# Add the project folder to the front of sys.path, so Python can find our own
# files: config.py, and the graph/ folder.
#   Path(__file__)   this file:   .../run_query.py
#   .parent          its folder:  the project folder
# ONE ".parent", because this file sits directly in the project folder.

import config
# Our settings file. We use one setting from it:
#   config.COMPANIES   ["Eternal", "Infosys"]: to check the company typed is one of ours

from graph.build_graph import build_graph
# The function that builds our whole pipeline, from build_graph.py.

from graph.state import create_initial_state
# The function that makes a fresh, empty state for a new question, from state.py.


# ── THE DEFAULTS ──────────────────────────────────────────────────────────────

DEFAULT_QUESTION = "What was the operating margin in FY 2026 ? "
# The question we use if none is typed on the command line.
# It's a COMPARISON question, which is what our app is built for. And it
# contains the word "risk", so topic.py allows it by keyword, with no Azure call.

DEFAULT_COMPANY = "Eternal"
# The company we use if none is typed.


# ── PRINTING HELPERS ──────────────────────────────────────────────────────────
# These four functions only PRINT. They don't change the state, and they
# don't call Azure. They make each stage of the run easy to read.

def heading(title: str) -> None:
    # Takes a title. Prints it between two lines of "=" signs.
    """Print a section heading."""
    # The function's one-line description.

    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    # Print: a blank line, a line of 74 "=" signs, the title, and another
    # line of 74 "=" signs.
    #   '=' * 74   repeats "=" 74 times
    #   \n         a new line


def show_retrieval(state: dict) -> None:
    # Takes the state after the run. Prints, for each year, the 5 chunks the
    # semantic ranker kept, and where hybrid search had put each one.
    """Show, per year, what the semantic ranker kept and where hybrid search had ranked it."""
    # The function's one-line description.

    heading("RETRIEVAL  (Azure AI Search: hybrid search, then semantic ranker)")
    # Print the heading for this section.

    for year, kept in sorted(state["reranked_chunks"].items()):
        # Go through each year's kept chunks, in order: FY2025, then FY2026.
        #   state["reranked_chunks"]   {"FY2025": [5 chunks], "FY2026": [5 chunks]}
        #   .items()                   each year with its chunks
        #   sorted(...)                in year order

        candidates = state["candidate_chunks"][year]
        # This year's 20 candidates from hybrid search, in hybrid order.

        hybrid_rank = {id(c): i + 1 for i, c in enumerate(candidates)}
        # A lookup: each chunk's position in the HYBRID list (1 to 20).
        #   enumerate(candidates)   gives each chunk with its position: (0, chunk), ...
        #   i + 1                   count from 1, not 0
        #   id(c)                   Python's unique number for that chunk object
        # rerank kept the SAME chunk objects, so id() finds each one's hybrid
        # position. (The same trick as show_comparison() in rerank.py.)

        print(f"\n  {year}: {len(candidates)} candidates -> {len(kept)} kept")
        # e.g.  "FY2025: 20 candidates -> 5 kept"

        for new_rank, chunk in enumerate(kept, start=1):
            # Go through the 5 kept chunks, best first.
            #   new_rank   its position after the semantic ranker: 1 to 5

            print(f"    #{new_rank} (was #{hybrid_rank[id(chunk)]:>2})  semantic {chunk['reranker_score'] or 0:.2f}  "
                  f"{chunk['text'][:50].replace(chr(10), ' ')}")
            # Print one line per kept chunk:
            #   #1 (was # 4)  semantic 2.74  ###### **Market risk**   The Company operates...
            #
            # The parts:
            #   #{new_rank}                    its position now, after the semantic ranker
            #   (was #{...:>2})                its position in hybrid search.
            #                                  :>2 right-aligns it in 2 spaces, so
            #                                  "# 4" and "#12" line up
            #   chunk['reranker_score'] or 0   the semantic score (0 if missing)
            #   :.2f                           2 digits after the point
            #   chunk['text'][:50]             the first 50 characters of the chunk
            #   .replace(chr(10), ' ')         new lines become spaces, so each
            #                                  chunk stays on one line


def show_grounding(state: dict) -> None:
    # Takes the state after the run. Prints what the output guardrails decided.
    """Show what the output guardrail concluded."""
    # The function's one-line description.

    heading("OUTPUT CHECKS  (format, then grounding)")
    # Print the heading for this section.

    print(f"  verdict   {state['grounding_verdict']}")
    # The verdict: grounded, needs_review or ungrounded.

    print(f"  score     {state['grounding_score'] or 0:.2f}")
    # The share of the answer's numbers that traced back, from 0 to 1.
    #   or 0    if the score is None, print 0 instead of crashing
    #   :.2f    2 digits after the point, e.g. 1.00

    for item in state.get("unsupported_items") or []:
        # Go through every problem the checks found.
        #   .get(...)   gives back None if the field is missing
        #   or []       if there's nothing, use an empty list, so the loop
        #               simply doesn't run

        print(f"  flagged   {item[:64]}")
        # Print one line per problem, cut to 64 characters, e.g.
        #   flagged   21.1 (attributed to FY2026, found only in another year)
        #   flagged   format: answer does not mention FY2025


def ask_reviewer(state: dict) -> dict:
    # Plays the HUMAN REVIEWER, in the terminal.
    # Shows the draft, why it was flagged, and the evidence. Then asks you to
    # approve or reject it. Gives back your decision, as fields for the state.
    """Show the reviewer the draft, the reasons and the evidence, then collect a decision."""
    # The function's one-line description.

    heading("HUMAN REVIEW REQUIRED -- nothing has been shown to the user")
    # Print the heading. Here, we can notice the important promise in it:
    # NOTHING has been shown to the user yet. The graph is paused.

    print(f"\nDRAFT ANSWER\n\n{state['draft_answer']}\n")
    # Show the draft: what gpt-5.1 wrote, before anyone approved it.

    print("WHY IT WAS FLAGGED")
    # A sub-heading.

    for item in state.get("unsupported_items") or ["(no specific item)"]:
        # Go through every problem found. If there are none listed, show
        # "(no specific item)" instead, so the reviewer isn't left guessing.

        print(f"  - {item}")
        # One line per problem, in full this time (not cut to 64 characters).

    print("\nTHE EXTRACTS IT SHOULD HAVE COME FROM")
    # A sub-heading: the evidence the answer was supposed to use.

    for year, chunks in sorted(state["reranked_chunks"].items()):
        # Go through each year's kept chunks, in order.

        print(f"  {year}")
        # The year, e.g. "FY2025".

        for chunk in chunks[:3]:
            # The top THREE chunks of this year (chunks[:3] means the first
            # three), as a quick reminder. Not all five, to keep it short.

            print(f"    - {chunk['text'][:64].replace(chr(10), ' ')}...")
            # The first 64 characters of the chunk, on one line, then "...".

    decision = ""
    # The reviewer's answer. It starts empty.

    while decision not in ("a", "r"):
        # Keep asking until the answer is "a" or "r".
        # So a typo, like "x", just asks again instead of crashing.

        decision = input("\n  approve or reject? [a/r]: ").strip().lower()
        # input(...) shows the prompt, and WAITS for you to type and press Enter.
        #   .strip()   remove spaces at the start and end
        #   .lower()   "A" becomes "a", so capitals work too

    if decision == "r":
        # Did you reject it?

        return {"review_decision": "rejected"}
        # Then give back just the decision. human_review_node will show the
        # user an honest "I wasn't able to answer that reliably" message.

    note = input("  corrected answer (or press enter to approve as written): ").strip()
    # You approved it. Now, optionally, type a corrected answer.
    # Press Enter with nothing typed to approve the draft as it is.

    return {"review_decision": "approved", "review_note": note or None}
    # Give back the decision, and the correction if you typed one.
    #   note or None   an empty text counts as "nothing", so it becomes None.
    #                  human_review_node then uses the draft as written.


# ── THE RUN ───────────────────────────────────────────────────────────────────

def run(question: str, company: str) -> None:
    # Puts ONE question through the whole graph, printing each stage.
    """Put one question through the graph, pausing for review if needed."""
    # The function's one-line description.

    graph = build_graph()
    # Build the pipeline: 7 nodes, the edges, the checkpointer, and the
    # pause before human_review.

    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    # A THREAD: the label for this one run.
    #   uuid.uuid4()   a brand-new unique id, e.g. "3f2a9c1e-..."
    # The checkpointer saves this run's state under that id. When we resume
    # after the pause, we pass the same thread, so it finds THIS run's state.

    heading("QUESTION")
    # Print the first heading.

    print(f"  {question}\n  ({company})")
    # Print the question, and the company on the next line.

    state = graph.invoke(create_initial_state(question, company), thread)
    # RUN THE GRAPH.
    #   create_initial_state(...)   a fresh state, with the question and company
    #   graph.invoke(state, thread) run the nodes, one after another
    #
    # It stops in one of two ways:
    #   - it reaches END: the run is finished
    #   - it reaches the PAUSE before human_review: the run is waiting
    # Either way, it gives back the state as it is at that moment.

    if not state["input_allowed"]:
        # Did an input guardrail refuse the question?

        heading("BLOCKED BY AN INPUT GUARDRAIL")
        # Then print this heading...

        print(f"  reason (internal): {state['input_block_reason']}\n\n{state['final_answer']}")
        # ...the reason (for us, not for the user), then a blank line, then
        # the polite message the user would see.

        return
        # And stop here. Nothing else ran: no search, no gpt-5.1.

    show_retrieval(state)
    # Print what search and the semantic ranker did.

    show_grounding(state)
    # Print what the output guardrails concluded.

    if graph.get_state(thread).next:
        # Is the graph PAUSED?
        #   graph.get_state(thread)   the saved state of this run
        #   .next                     the node(s) waiting to run next.
        #                             ("human_review",) means: paused for review.
        #                             An empty tuple () means: finished.
        # An empty tuple counts as "nothing", so this is True only when paused.

        graph.update_state(thread, ask_reviewer(state))
        # Ask you to review it (ask_reviewer), then WRITE your decision into
        # the saved state of this run.
        # This is the same thing the web app does when the reviewer clicks a button.

        state = graph.invoke(None, thread)
        # RESUME the graph.
        #   None     don't start a new run: carry on from where it paused
        #   thread   which run to carry on
        # Now human_review_node runs, reads your decision, and sets the final answer.

    heading("ANSWER SHOWN TO THE USER")
    # Print the last heading.

    print(state["final_answer"])
    # Print the final answer: what the user would actually see.


# ── RUN IT ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run the code below ONLY when this file is run directly:
    #   python run_query.py
    # If another file imports this one, nothing below runs.

    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    # The question: the FIRST thing typed after the file name, if anything was.
    #   sys.argv      the words on the command line:
    #                 ["run_query.py", "How did revenue change?", "Eternal"]
    #   sys.argv[1]   the first word after the file name (in quotes, a whole
    #                 question counts as one word)
    #   len(sys.argv) > 1   was anything typed after the file name?
    # If not, use the default question.

    company = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_COMPANY
    # The company: the SECOND thing typed, if anything was. Otherwise "Infosys".

    if company not in config.COMPANIES:
        # Is it one of our companies? (Capitals matter: "Eternal", not "eternal".)

        sys.exit(f"Unknown company: {company}. Available: {', '.join(config.COMPANIES)}")
        # No: stop, with a message that lists the companies we do have:
        #   Unknown company: Tata. Available: Eternal, Infosys

    run(question, company)
    # Put the question through the pipeline.