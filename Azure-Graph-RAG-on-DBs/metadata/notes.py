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

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from adapters.postgres import Catalog, PostgresAdapter, Table
from metadata import llm
from metadata.links import Link

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


def fingerprint(table: Table) -> str:
    """
    A short hash of the table's column names and types. If a later run
    produces the same fingerprint, the table has not changed and its note
    does not need redrafting.
    """
    text = "|".join(f"{c.name}:{c.data_type}" for c in table.columns)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _describe_for_model(table: Table, stats: dict, links: list[Link], pii: set,
                        adapter: PostgresAdapter, n_samples: int) -> str:
    """Build the text the model reads for one table."""
    lines = [f"Table: {table.name}", f"Estimated rows: {table.row_estimate:,}",
             f"Primary key: {', '.join(table.primary_key) or 'none'}", "", "Columns:"]
    for column in table.columns:
        s = stats.get((table.name, column.name))
        facts = [column.data_type]
        if s:
            facts.append(f"{s.null_fraction:.0%} empty")
            facts.append(f"~{s.distinct_values:,.0f} distinct values")
        if (table.name, column.name) in pii:
            facts.append("PERSONAL DATA - values withheld")
        else:
            # Prefer the most common values from pg_stats; if there are none
            # (e.g. every value is different), read a few real ones instead.
            examples = (s.common_values if s and s.common_values else
                        adapter.sample_values(table.name, column.name, n_samples))[:n_samples]
            if examples:
                facts.append("examples: " + ", ".join(examples))
        lines.append(f"  - {column.name}: " + "; ".join(facts))

    related = [lk for lk in links if table.name in (lk.from_table, lk.to_table)]
    if related:
        lines += ["", "Links:"]
        for lk in related:
            kind = "declared foreign key" if lk.source == "declared" else "undeclared, found by matching values"
            lines.append(f"  - {lk.from_table}.{lk.from_column} -> {lk.to_table}.{lk.to_column} ({kind})")
    return "\n".join(lines)


def draft(adapter: PostgresAdapter, catalog: Catalog, stats: dict, links: list[Link],
          pii: set, tables_to_draft: list[str], settings: dict) -> dict[str, dict]:
    """
    Draft notes for the named tables, several at a time.
    Returns table name -> the model's JSON answer.
    """
    def one(table_name: str) -> tuple[str, dict]:
        prompt = _describe_for_model(catalog.tables[table_name], stats, links, pii,
                                     adapter, settings["sample_values"])
        answer = llm.chat_json("strong", SYSTEM_PROMPT, prompt)
        # Keep only descriptions for columns that really exist, so a
        # made-up column name from the model can never be saved.
        real = {c.name for c in catalog.tables[table_name].columns}
        answer["columns"] = {k: v for k, v in answer.get("columns", {}).items() if k in real}
        print(f"  drafted {table_name}")
        return table_name, answer

    # Draft several tables at once. Each call mostly waits on Azure, so
    # running a few in parallel cuts the total time without extra cost.
    with ThreadPoolExecutor(max_workers=settings["parallel_calls"]) as pool:
        return dict(pool.map(one, tables_to_draft))


def to_json(notes: dict) -> str:
    """Pretty-print notes, used by review.py."""
    return json.dumps(notes, indent=2, ensure_ascii=False)