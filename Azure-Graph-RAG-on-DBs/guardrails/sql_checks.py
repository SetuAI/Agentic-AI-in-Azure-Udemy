"""
guardrails/sql_checks.py
========================

WHAT THIS FILE IS FOR
---------------------
Checks SQL written by the model BEFORE it runs. Uses sqlglot, which reads
SQL text into a tree, so the checks look at the real structure of the query
rather than searching the text for words (which is easy to fool).

    1. exactly one statement           -> no "SELECT ...; DROP ..." tricks
    2. it only reads                   -> SELECT / WITH ... SELECT only, nothing that writes
    3. only known tables               -> every table must be one of the 20 in the catalog
    4. date filter where required      -> transactions queries must filter on txn_date

The cost check (EXPLAIN) runs separately, in the agent, because it needs
the database. The database also refuses writes on its own: queries run in a
READ ONLY transaction (adapters/postgres.py). That is the backstop if these
checks ever missed something.
"""

# sqlglot: parses SQL text into a tree.
import sqlglot

# exp: the tree's node types (Select, Table, Where, Insert, ...).
from sqlglot import exp

# Anything in this list anywhere in the query means it would change something.
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
               exp.TruncateTable, exp.Merge, exp.Command, exp.Grant)


def check(sql: str, known_tables: set[str], date_filter_required: dict[str, str]) -> list[dict]:
    """
    Returns one entry per check: {"check": name, "passed": bool, "detail": text}.
    The query may run only if every entry passed.
    """
    # One entry per check, filled in by record().
    results = []

    # Helper: save one check's result, and return whether it passed.
    def record(name, passed, detail):
        # Add the entry.
        results.append({"check": name, "passed": passed, "detail": detail})
        # Lets "if not record(...)" stop early on a failure.
        return passed

    # 1. Parse. read="postgres" understands Postgres syntax such as ::date.
    # Try to read the SQL; a syntax error jumps to "except".
    try:
        # parse() returns one tree per statement; empty ones (None) are dropped.
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    # The SQL could not be read: record the failure and stop.
    except sqlglot.errors.ParseError as error:
        record("parses", False, f"Not valid SQL: {error}")
        return results
    # Exactly one statement allowed; otherwise stop.
    if not record("single_statement", len(statements) == 1, f"{len(statements)} statement(s)"):
        return results
    # The one statement's tree.
    tree = statements[0]

    # 2. Read only: the top must be a SELECT (or UNION etc.), and nothing
    # inside it may write.
    # True if the top of the tree is a query (SELECT, UNION, ...).
    is_query = isinstance(tree, exp.Query)
    # walk() visits every node; collect the names of any writing nodes.
    writes = [type(n).__name__ for n in tree.walk() if isinstance(n, WRITE_NODES)]
    # Pass only if it is a query AND nothing inside writes.
    record("read_only", is_query and not writes,
           "SELECT only" if is_query and not writes else f"Not allowed: {writes or type(tree).__name__}")

    # 3. Known tables only. Names defined in WITH (CTEs) are the query's own
    # temporary results, not real tables, so they are left out.
    # Names created with WITH name AS (...).
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    # Every table name in the query, minus those WITH names.
    used = {t.name for t in tree.find_all(exp.Table)} - cte_names
    # Tables we do not know about (sorted for a stable message).
    unknown = sorted(used - known_tables)
    # Pass only if there are no unknown tables.
    record("known_tables", not unknown, f"Tables: {', '.join(sorted(used))}" if not unknown
           else f"Unknown tables: {', '.join(unknown)}")

    # 4. Date filter on big partitioned tables. Some WHERE clause must COMPARE
    # the date column with a value (>=, <, BETWEEN, =, ...). Merely
    # mentioning it is not enough: "txn_date IS NOT NULL" is true for every
    # row, so it filters nothing and Postgres would still read all 60 months.
    # Node types that compare: >, >=, <, <=, =, BETWEEN, IN.
    comparisons = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between, exp.In)
    # e.g. ("transactions", "txn_date") from config/agent.yaml.
    for table, date_column in date_filter_required.items():
        # Only checked when the query actually reads that table.
        if table in used:
            # True if any comparison inside any WHERE mentions the date column.
            filtered = any(
                any(col.name == date_column for col in cmp.find_all(exp.Column))
                for where in tree.find_all(exp.Where) for cmp in where.find_all(*comparisons)
            )
            # Record the result with a helpful message.
            record("date_filter", filtered, f"{table} filtered by a range on {date_column}" if filtered
                   else f"{table} must be filtered by a date range on {date_column} (e.g. >= and <)")
    # All check results.
    return results


def tables_in(sql: str) -> list[str]:
    """The real tables a query reads (not its WITH names). Empty if it does not parse."""
    # A query that cannot be parsed reads no known tables.
    try:
        # parse_one: parse a single statement.
        tree = sqlglot.parse_one(sql, read="postgres")
    # Not valid SQL.
    except sqlglot.errors.ParseError:
        return []
    # WITH names, left out as above.
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    # The real table names, sorted.
    return sorted({t.name for t in tree.find_all(exp.Table)} - cte_names)