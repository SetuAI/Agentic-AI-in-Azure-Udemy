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

# argparse: reads command-line options such as --approve-all.
import argparse

# os: reads AGENT_DB_URL.
import os

# textwrap: wraps long descriptions to 100 characters for the terminal.
import textwrap

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# Connects to agent_meta.
from metadata import store


# Print every table note with its column notes.
def show_notes(conn) -> None:
    # One row per table, sorted by name.
    for name, status, rows, description, grain, terms in conn.execute(
        # SQL: every table note.
        "SELECT table_name, status, row_estimate, description, grain, business_terms FROM meta_tables ORDER BY table_name"
    ).fetchall():
        # Table heading, e.g. "=== schemes  [draft]  ~40 rows".
        print(f"\n=== {name}  [{status}]  ~{rows:,} rows")
        # The description, wrapped and indented.
        print(textwrap.fill(description or "(no description)", 100, initial_indent="  ", subsequent_indent="  "))
        # What one row represents.
        print(f"  Grain: {grain}")
        # Words users might say for this table.
        print(f"  Business terms: {', '.join(terms or [])}")
        # Then every column note of that table.
        for column, data_type, is_pii, note in conn.execute(
            # SQL: that table's column notes.
            "SELECT column_name, data_type, is_pii, description FROM meta_columns WHERE table_name = %s",
            # The value for %s: the table name.
            (name,),
        ).fetchall():
            # Mark personal-data columns.
            flag = " [PII]" if is_pii else ""
            # e.g. "    - rtg_dt (date): The date the rating was given."
            print(f"    - {column} ({data_type}){flag}: {note}")


# Print every link as a numbered table.
def show_links(conn) -> None:
    # Section heading.
    print("\n=== Links")
    # Column headings (> right-aligns, < left-aligns, the number is the width).
    print(f"  {'#':>3}  {'status':<9} {'source':<14} {'conf':>5}  link")
    # Undeclared links first (False sorts before True), then by ID.
    for edge_id, status, source, confidence, a, ac, b, bc, evidence in conn.execute(
        # SQL: every link, with its status, source, confidence and evidence.
        """SELECT edge_id, status, source, confidence, from_table, from_column, to_table, to_column, evidence
           FROM meta_edges ORDER BY (source = 'declared'), edge_id"""
    ).fetchall():
        # One line per link. Use the number in the "#" column with --reject-link.
        print(f"  {edge_id:>3}  {status:<9} {source:<14} {float(confidence):>5.2f}  {a}.{ac} -> {b}.{bc}")
        # Found links also show their evidence underneath.
        if source != "declared":
            # Indented under the link line.
            print(f"{'':>38}{evidence}")


# Run from the terminal: uv run python -m metadata.review [options]
def main() -> None:
    # Describe the options (shown by --help).
    parser = argparse.ArgumentParser(description="Review and approve drafted notes and proposed links.")
    # --links: show only the links (skip the notes).
    parser.add_argument("--links", action="store_true", help="show only the links")
    # --reject-link ID may be given several times; each ID is added to a list.
    parser.add_argument("--reject-link", type=int, action="append", default=[], metavar="ID")
    # --approve-all: approve everything still pending.
    parser.add_argument("--approve-all", action="store_true", help="approve every pending note and link")
    # Read the options actually typed.
    args = parser.parse_args()

    # Read .env.
    load_dotenv()
    # Connect to agent_meta.
    conn = store.connect(os.environ["AGENT_DB_URL"])

    # Reject every link asked for.
    for edge_id in args.reject_link:
        # Mark this link as rejected.
        conn.execute("UPDATE meta_edges SET status = 'rejected' WHERE edge_id = %s", (edge_id,))
        # Confirm.
        print(f"Rejected link {edge_id}.")

    # --approve-all: approve every draft note and every proposed link, then stop.
    if args.approve_all:
        # .rowcount = how many rows the UPDATE changed.
        notes = conn.execute(
            # SQL: approve every draft note and stamp the time.
            "UPDATE meta_tables SET status = 'approved', approved_at = now() WHERE status = 'draft'").rowcount
        # Approve every proposed link; .rowcount = how many changed.
        links = conn.execute("UPDATE meta_edges SET status = 'approved' WHERE status = 'proposed'").rowcount
        # Report and point to the next step.
        print(f"Approved {notes} table notes and {links} links. Next: uv run python -m metadata.publish")
        # Stop here; nothing is shown.
        return

    # Without --links, show the notes too.
    if not args.links:
        # Print the table and column notes.
        show_notes(conn)
    # Always show the links.
    show_links(conn)

    # How many notes and links still wait for a decision.
    pending_notes = conn.execute("SELECT count(*) FROM meta_tables WHERE status = 'draft'").fetchone()[0]
    # Links still waiting.
    pending_links = conn.execute("SELECT count(*) FROM meta_edges WHERE status = 'proposed'").fetchone()[0]
    # Report what is still pending.
    print(f"\nPending: {pending_notes} notes, {pending_links} links.")
    # Close the connection.
    conn.close()


# True only when run as: uv run python -m metadata.review
if __name__ == "__main__":
    # Start the review.
    main()