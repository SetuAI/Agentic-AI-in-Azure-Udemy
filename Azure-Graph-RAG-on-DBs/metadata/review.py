"""
metadata/review.py
==================

WHAT THIS FILE IS FOR
---------------------
Step 2 of 3. Shows a person everything build.py drafted, so they can check
it before anything reaches the SQL-writing model.

    uv run python -m metadata.review                     # show notes and links
    uv run python -m metadata.review --links             # show only the links
    uv run python -m metadata.review --reject-link 7     # reject link number 7
    uv run python -m metadata.review --approve-all       # approve everything still pending

WHAT TO LOOK FOR
----------------
    Notes: does each description match what the table really holds? Are the
           cryptic columns (rtg_dt, arn_no, ...) explained correctly?
    Links: is each proposed link real? A wrong link produces wrong joins,
           and wrong joins produce confidently wrong answers.

--approve-all only approves items that are still pending. Anything you
rejected stays rejected, so the usual flow is: read, reject the bad ones,
then approve the rest.

In the full build, this review happens through an API endpoint for
reviewers. This command is the same approval step, run from the terminal.
"""

import argparse
import os
import textwrap

from dotenv import load_dotenv

from metadata import store


def show_notes(conn) -> None:
    for name, status, rows, description, grain, terms in conn.execute(
        "SELECT table_name, status, row_estimate, description, grain, business_terms FROM meta_tables ORDER BY table_name"
    ).fetchall():
        print(f"\n=== {name}  [{status}]  ~{rows:,} rows")
        print(textwrap.fill(description or "(no description)", 100, initial_indent="  ", subsequent_indent="  "))
        print(f"  Grain: {grain}")
        print(f"  Business terms: {', '.join(terms or [])}")
        for column, data_type, is_pii, note in conn.execute(
            "SELECT column_name, data_type, is_pii, description FROM meta_columns WHERE table_name = %s",
            (name,),
        ).fetchall():
            flag = " [PII]" if is_pii else ""
            print(f"    - {column} ({data_type}){flag}: {note}")


def show_links(conn) -> None:
    print("\n=== Links")
    print(f"  {'#':>3}  {'status':<9} {'source':<14} {'conf':>5}  link")
    for edge_id, status, source, confidence, a, ac, b, bc, evidence in conn.execute(
        """SELECT edge_id, status, source, confidence, from_table, from_column, to_table, to_column, evidence
           FROM meta_edges ORDER BY (source = 'declared'), edge_id"""
    ).fetchall():
        print(f"  {edge_id:>3}  {status:<9} {source:<14} {float(confidence):>5.2f}  {a}.{ac} -> {b}.{bc}")
        if source != "declared":
            print(f"{'':>38}{evidence}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review and approve drafted notes and proposed links.")
    parser.add_argument("--links", action="store_true", help="show only the links")
    parser.add_argument("--reject-link", type=int, action="append", default=[], metavar="ID")
    parser.add_argument("--approve-all", action="store_true", help="approve every pending note and link")
    args = parser.parse_args()

    load_dotenv()
    conn = store.connect(os.environ["AGENT_DB_URL"])

    for edge_id in args.reject_link:
        conn.execute("UPDATE meta_edges SET status = 'rejected' WHERE edge_id = %s", (edge_id,))
        print(f"Rejected link {edge_id}.")

    if args.approve_all:
        notes = conn.execute(
            "UPDATE meta_tables SET status = 'approved', approved_at = now() WHERE status = 'draft'").rowcount
        links = conn.execute("UPDATE meta_edges SET status = 'approved' WHERE status = 'proposed'").rowcount
        print(f"Approved {notes} table notes and {links} links. Next: uv run python -m metadata.publish")
        return

    if not args.links:
        show_notes(conn)
    show_links(conn)

    pending_notes = conn.execute("SELECT count(*) FROM meta_tables WHERE status = 'draft'").fetchone()[0]
    pending_links = conn.execute("SELECT count(*) FROM meta_edges WHERE status = 'proposed'").fetchone()[0]
    print(f"\nPending: {pending_notes} notes, {pending_links} links.")
    conn.close()


if __name__ == "__main__":
    main()