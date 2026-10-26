"""
metadata/notes.py
=================

WHAT THIS FILE IS FOR
---------------------
Asks the strong model (gpt-5.1) to draft a plain-English note for each table
and each of its columns. This is tradeoff 3.

These notes are what the SQL-writing model reads in Stage 6 to decide which
tables and columns to use. A good note is the difference between the model
guessing what "rtg_dt" means and knowing it is the date a rating was given.

WHAT THE MODEL IS SHOWN, PER TABLE
----------------------------------
    - table name and estimated row count
    - each column's name, type, null rate and number of distinct values
    - a few example values per column, EXCEPT for PII columns (config/pii.yaml)
    - the links to and from this table (declared and proposed)

WHAT THE MODEL IS NEVER SHOWN
-----------------------------
    - values from PII columns
    - the answer key, or anything about planted patterns

WHAT COMES BACK
---------------
A JSON object: a description of the table, what one row represents (its
"grain"), a short list of business terms, and one description per column.
Everything is saved as 'draft' until a human approves it in review.py.
"""

# hashlib: makes the short fingerprint hash.
import hashlib

# json: pretty-prints notes for review.
import json

# Runs several LLM calls at the same time.
from concurrent.futures import ThreadPoolExecutor

# Types from the adapter.
from adapters.postgres import Catalog, PostgresAdapter, Table

# Calls Azure OpenAI.
from metadata import llm

# The Link type (one link between two tables).
from metadata.links import Link

# The instructions given to the strong model for every table.
SYSTEM_PROMPT = """You document tables in the relational database of an Indian mutual fund company
(an AMC). Your notes are read by another AI model that writes SQL, and by people new to the domain.

Write plainly and precisely. Column names may be abbreviated or cryptic: work out their meaning from
the name, type, example values and links, and say what they mean in full. Mention units (rupees,
percent, count) and what empty values mean where it matters. Do not invent columns or values.

Reply with a JSON object with exactly these keys:
  "description":     2-3 sentences on what the table holds and when you would use it
  "grain":           what one row represents, starting "One row per"
  "business_terms":  3-8 words or phrases a business user might say that point to this table
  "columns":         an object mapping every column name to a one-sentence description"""


# Used by build.py to skip tables whose columns have not changed.
def fingerprint(table: Table) -> str:
    """
    A short hash of the table's column names and types. If a later run
    produces the same fingerprint, the table has not changed and its note
    does not need redrafting.
    """
    # e.g. "scheme_id:integer|scheme_name:text|..."
    text = "|".join(f"{c.name}:{c.data_type}" for c in table.columns)
    # SHA-256 hash of that text, as hex, cut to 16 characters.
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# Writes one table's facts as plain text for the LLM.
def _describe_for_model(table: Table, stats: dict, links: list[Link], pii: set,
                        # adapter: used to read sample values; n_samples: how many examples to show.
                        adapter: PostgresAdapter, n_samples: int) -> str:
    """Build the text the model reads for one table."""
    # The first lines: name, row estimate, primary key, then a "Columns:" heading.
    lines = [f"Table: {table.name}", f"Estimated rows: {table.row_estimate:,}",
             # Primary key columns, a blank line, then the "Columns:" heading.
             f"Primary key: {', '.join(table.primary_key) or 'none'}", "", "Columns:"]
    # One line per column.
    for column in table.columns:
        # The column's statistics (None if none).
        s = stats.get((table.name, column.name))
        # Facts about the column, starting with its type.
        facts = [column.data_type]
        # Add the empty-value rate and the number of distinct values.
        if s:
            # e.g. "3% empty".
            facts.append(f"{s.null_fraction:.0%} empty")
            # e.g. "~40 distinct values".
            facts.append(f"~{s.distinct_values:,.0f} distinct values")
        # PII column: say so, and show NO values.
        if (table.name, column.name) in pii:
            # Never show example values of a PII column.
            facts.append("PERSONAL DATA - values withheld")
        # Any other column: show some example values.
        else:
            # Prefer the most common values from pg_stats; if there are none
            # (e.g. every value is different), read a few real ones instead.
            examples = (s.common_values if s and s.common_values else
                        # ... otherwise read a few real values; keep at most n_samples.
                        adapter.sample_values(table.name, column.name, n_samples))[:n_samples]
            # Only if there are any.
            if examples:
                # e.g. "examples: Equity, Debt, Hybrid".
                facts.append("examples: " + ", ".join(examples))
        # e.g. "  - rtg_dt: date; 0% empty; ~900 distinct values; examples: ..."
        lines.append(f"  - {column.name}: " + "; ".join(facts))

    # Links that start or end at this table.
    related = [lk for lk in links if table.name in (lk.from_table, lk.to_table)]
    # Add a "Links:" section listing them.
    if related:
        # A blank line, then a "Links:" heading.
        lines += ["", "Links:"]
        # One line per link.
        for lk in related:
            # Say how each link was found.
            kind = "declared foreign key" if lk.source == "declared" else "undeclared, found by matching values"
            # e.g. "  - transactions.arn_code -> distributors.arn_no (undeclared, ...)".
            lines.append(f"  - {lk.from_table}.{lk.from_column} -> {lk.to_table}.{lk.to_column} ({kind})")
    # Join all lines into one text.
    return "\n".join(lines)


# Called by build.py with the tables that need notes.
def draft(adapter: PostgresAdapter, catalog: Catalog, stats: dict, links: list[Link],
          # tables_to_draft: which tables; settings: the "notes" section of metadata.yaml.
          pii: set, tables_to_draft: list[str], settings: dict) -> dict[str, dict]:
    """
    Draft notes for the named tables, several at a time.
    Returns table name -> the model's JSON answer.
    """
    # Draft ONE table (run in parallel by the pool below).
    def one(table_name: str) -> tuple[str, dict]:
        # The text describing this table.
        prompt = _describe_for_model(catalog.tables[table_name], stats, links, pii,
                                     # (the adapter and how many example values to show)
                                     adapter, settings["sample_values"])
        # Ask the strong model; returns a dict.
        answer = llm.chat_json("strong", SYSTEM_PROMPT, prompt)
        # Keep only descriptions for columns that really exist, so a
        # made-up column name from the model can never be saved.
        # The table's real column names.
        real = {c.name for c in catalog.tables[table_name].columns}
        # Keep only real columns.
        answer["columns"] = {k: v for k, v in answer.get("columns", {}).items() if k in real}
        # Progress line.
        print(f"  drafted {table_name}")
        # A (name, answer) pair.
        return table_name, answer

    # Draft several tables at once. Each call mostly waits on Azure, so
    # running a few in parallel cuts the total time without extra cost.
    # A pool of parallel_calls worker threads.
    with ThreadPoolExecutor(max_workers=settings["parallel_calls"]) as pool:
        # Run one() for every table; collect the (name, answer) pairs into a dict.
        return dict(pool.map(one, tables_to_draft))


# Turn notes into readable JSON text.
def to_json(notes: dict) -> str:
    """Pretty-print notes, used by review.py."""
    # indent=2: readable layout; ensure_ascii=False: keep characters like ₹ as they are.
    return json.dumps(notes, indent=2, ensure_ascii=False)