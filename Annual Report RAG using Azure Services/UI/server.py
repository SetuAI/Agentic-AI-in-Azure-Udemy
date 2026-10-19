"""
================================================================================
WEB UI - a small local server for the annual-report pipeline
================================================================================

Serves UI/index.html and exposes the LangGraph pipeline as two JSON endpoints:

    POST /api/query    {"question": "...", "company": "Infosys"}
                       -> runs the graph; returns chunks before and after
                          reranking, the grounding checks and the answer
                          (or a "paused" flag if a human must review it)

    POST /api/review   {"thread_id": "...", "decision": "approved"|"rejected",
                        "note": "optional corrected answer"}
                       -> resumes a paused run and returns the final answer

No extra packages: it uses Python's built-in http.server.

HOW TO RUN
----------
    python UI/server.py              then open http://localhost:8000
    python UI/server.py 8080         a different port
================================================================================
"""

import json                                     # request and response bodies
import sys                                      # command-line port, import path
import traceback                                # send readable errors to the browser
import uuid                                     # a unique id for each run
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # built-in web server
from pathlib import Path                        # Path builds file paths

UI_DIR = Path(__file__).parent                  # this folder: UI/
sys.path.insert(0, str(UI_DIR.parent))          # make the project folder importable

import config                                   # companies, years, top-k settings
from graph.build_graph import build_graph       # the pipeline
from graph.state import create_initial_state    # a fresh state

GRAPH = build_graph()                           # one compiled graph; its MemorySaver keeps paused runs


def chunk_key(chunk: dict) -> str:
    """A stable id for a chunk: its year plus its position in the report."""
    return f"{chunk['fiscal_year']}:{chunk['position']}"


def describe_retrieval(state: dict) -> dict:
    """Per year: every candidate in hybrid order, with where the semantic ranker put it (if kept)."""
    years = {}
    for year in sorted((state.get("candidate_chunks") or {}).keys()):
        candidates = state["candidate_chunks"][year]                     # hybrid order
        kept = (state.get("reranked_chunks") or {}).get(year, [])        # semantic order
        semantic_rank = {chunk_key(c): i + 1 for i, c in enumerate(kept)}
        years[year] = {
            "candidates": [
                {
                    "key": chunk_key(c),
                    "hybrid_rank": i + 1,
                    "semantic_rank": semantic_rank.get(chunk_key(c)),   # None = dropped by the ranker
                    "hybrid_score": c["score"],
                    "reranker_score": c["reranker_score"],
                    "position": c["position"],
                    "year_label": c["year_label"],
                    "text": c["text"],
                }
                for i, c in enumerate(candidates)
            ],
            "kept_keys": [chunk_key(c) for c in kept],                   # semantic order, best first
        }
    return years


def describe(state: dict, thread_id: str) -> dict:
    """Everything the page needs from one graph state."""
    paused = bool(GRAPH.get_state({"configurable": {"thread_id": thread_id}}).next)
    return {
        "thread_id": thread_id,
        "question": state["question"],
        "company": state["company"],
        "input_allowed": state["input_allowed"],
        "input_block_reason": state["input_block_reason"],
        "years": describe_retrieval(state),
        "draft_answer": state["draft_answer"],
        "grounding_verdict": state["grounding_verdict"],
        "grounding_score": state["grounding_score"],
        "unsupported_items": state["unsupported_items"] or [],
        "paused": paused,                       # True = waiting for a human decision
        "review_decision": state["review_decision"],
        "final_answer": state["final_answer"],
    }


def run_query(body: dict) -> dict:
    question = (body.get("question") or "").strip()
    company = body.get("company") or config.COMPANIES[0]
    if not question:
        raise ValueError("Please type a question.")
    if company not in config.COMPANIES:
        raise ValueError(f"Unknown company: {company}")
    thread_id = str(uuid.uuid4())
    state = GRAPH.invoke(create_initial_state(question, company), {"configurable": {"thread_id": thread_id}})
    return describe(state, thread_id)


def resume_review(body: dict) -> dict:
    thread_id = body.get("thread_id")
    decision = body.get("decision")
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    thread = {"configurable": {"thread_id": thread_id}}
    if not GRAPH.get_state(thread).next:
        raise ValueError("That run is not waiting for review (the server may have restarted).")
    GRAPH.update_state(thread, {"review_decision": decision, "review_note": (body.get("note") or "").strip() or None})
    state = GRAPH.invoke(None, thread)          # None = resume where it stopped
    return describe(state, thread_id)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)  # serve files from UI/

    def do_GET(self):
        if self.path == "/api/config":
            return self.send_json({"companies": config.COMPANIES, "fiscal_years": config.FISCAL_YEARS,
                                   "candidates_per_year": config.VECTOR_SEARCH_TOP_K,
                                   "kept_per_year": config.RERANK_TOP_K})
        return super().do_GET()

    def do_POST(self):
        routes = {"/api/query": run_query, "/api/review": resume_review}
        if self.path not in routes:
            return self.send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(routes[self.path](body))
        except ValueError as exc:               # bad input: show the message as is
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:                # anything else: show it, and log the traceback
            traceback.print_exc()
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def send_json(self, payload: dict, status: int = 200):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"Annual-report RAG UI on http://localhost:{port}   (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
