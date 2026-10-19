"""
================================================================================
ASK A QUESTION - the whole pipeline, with human review at the keyboard
================================================================================

    python run_query.py
    python run_query.py "How did revenue change between the two years?"
    python run_query.py "How did revenue change between the two years?" Eternal

What you will see:
    - BLOCKED, if a guardrail refused the question
    - the retrieved chunks per year, and what the semantic ranker changed
    - the grounding verdict
    - if the answer could not be verified, the graph PAUSES and asks YOU to
      approve, correct or reject it before anything is shown
    - the final answer
================================================================================
"""

import sys                                      # read the command-line words
import uuid                                     # make a unique id for each run
from pathlib import Path                        # Path builds file paths

sys.path.insert(0, str(Path(__file__).parent))  # make azure_based/ importable

import config                                   # company list
from graph.build_graph import build_graph       # the pipeline
from graph.state import create_initial_state    # a fresh state

DEFAULT_QUESTION = "How did the company's disclosure of currency risk change between the two years?"  # a comparison
DEFAULT_COMPANY = "Infosys"                     # one of the two companies


def heading(title: str) -> None:
    """Print a section heading."""
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")  # a line, the title, a line


def show_retrieval(state: dict) -> None:
    """Show, per year, what the semantic ranker kept and where hybrid search had ranked it."""
    heading("RETRIEVAL  (Azure AI Search: hybrid search, then semantic ranker)")
    for year, kept in sorted(state["reranked_chunks"].items()):  # each year, in order
        candidates = state["candidate_chunks"][year]             # what hybrid search found
        hybrid_rank = {id(c): i + 1 for i, c in enumerate(candidates)}  # each chunk's hybrid position
        print(f"\n  {year}: {len(candidates)} candidates -> {len(kept)} kept")
        for new_rank, chunk in enumerate(kept, start=1):         # the kept chunks, best first
            print(f"    #{new_rank} (was #{hybrid_rank[id(chunk)]:>2})  semantic {chunk['reranker_score'] or 0:.2f}  "
                  f"{chunk['text'][:50].replace(chr(10), ' ')}")  # first 50 characters, newlines removed


def show_grounding(state: dict) -> None:
    """Show what the output guardrail concluded."""
    heading("OUTPUT CHECKS  (format, then grounding)")
    print(f"  verdict   {state['grounding_verdict']}")           # grounded / needs_review / ungrounded
    print(f"  score     {state['grounding_score'] or 0:.2f}")    # share of numbers traced
    for item in state.get("unsupported_items") or []:            # every problem found
        print(f"  flagged   {item[:64]}")


def ask_reviewer(state: dict) -> dict:
    """Show the reviewer the draft, the reasons and the evidence, then collect a decision."""
    heading("HUMAN REVIEW REQUIRED -- nothing has been shown to the user")
    print(f"\nDRAFT ANSWER\n\n{state['draft_answer']}\n")       # what the model wrote
    print("WHY IT WAS FLAGGED")
    for item in state.get("unsupported_items") or ["(no specific item)"]:  # the exact problems
        print(f"  - {item}")
    print("\nTHE EXTRACTS IT SHOULD HAVE COME FROM")
    for year, chunks in sorted(state["reranked_chunks"].items()):  # each year's evidence
        print(f"  {year}")
        for chunk in chunks[:3]:                                   # the top three, as a reminder
            print(f"    - {chunk['text'][:64].replace(chr(10), ' ')}...")

    decision = ""                               # keep asking until we get a or r
    while decision not in ("a", "r"):
        decision = input("\n  approve or reject? [a/r]: ").strip().lower()
    if decision == "r":                         # rejected
        return {"review_decision": "rejected"}
    note = input("  corrected answer (or press enter to approve as written): ").strip()  # optional correction
    return {"review_decision": "approved", "review_note": note or None}


def run(question: str, company: str) -> None:
    """Put one question through the graph, pausing for review if needed."""
    graph = build_graph()                       # the compiled pipeline
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}  # a unique id, so the checkpointer can find this run

    heading("QUESTION")
    print(f"  {question}\n  ({company})")

    state = graph.invoke(create_initial_state(question, company), thread)  # run until the end -- or the pause

    if not state["input_allowed"]:             # refused at the door?
        heading("BLOCKED BY AN INPUT GUARDRAIL")
        print(f"  reason (internal): {state['input_block_reason']}\n\n{state['final_answer']}")
        return                                  # nothing else ran

    show_retrieval(state)                       # what search and the semantic ranker did
    show_grounding(state)                       # what the checks concluded

    if graph.get_state(thread).next:            # is a node waiting to run? then the graph is paused
        graph.update_state(thread, ask_reviewer(state))  # write the reviewer's decision into the saved state
        state = graph.invoke(None, thread)      # None = resume where it stopped, not start again

    heading("ANSWER SHOWN TO THE USER")
    print(state["final_answer"])                # the final answer


if __name__ == "__main__":                      # only when run directly
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION  # first word after the script, if any
    company = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_COMPANY    # second word, if any
    if company not in config.COMPANIES:         # not one of ours?
        sys.exit(f"Unknown company: {company}. Available: {', '.join(config.COMPANIES)}")  # stop with a message
    run(question, company)                      # go
