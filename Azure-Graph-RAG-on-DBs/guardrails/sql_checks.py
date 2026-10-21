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

import sqlglot
from sqlglot import exp

# Anything in this list anywhere in the query means it would change something.
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
               exp.TruncateTable, exp.Merge, exp.Command, exp.Grant)


def check(sql: str, known_tables: set[str], date_filter_required: dict[str, str]) -> list[dict]:
    """
    Returns one entry per check: {"check": name, "passed": bool, "detail": text}.
    The query may run only if every entry passed.
    """
    results = []

    def record(name, passed, detail):
        results.append({"check": name, "passed": passed, "detail": detail})
        return passed

    # 1. Parse. read="postgres" understands Postgres syntax such as ::date.
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as error:
        record("parses", False, f"Not valid SQL: {error}")
        return results
    if not record("single_statement", len(statements) == 1, f"{len(statements)} statement(s)"):
        return results
    tree = statements[0]

    # 2. Read only: the top must be a SELECT (or UNION etc.), and nothing
    # inside it may write.
    is_query = isinstance(tree, exp.Query)
    writes = [type(n).__name__ for n in tree.walk() if isinstance(n, WRITE_NODES)]
    record("read_only", is_query and not writes,
           "SELECT only" if is_query and not writes else f"Not allowed: {writes or type(tree).__name__}")

    # 3. Known tables only. Names defined in WITH (CTEs) are the query's own
    # temporary results, not real tables, so they are left out.
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    used = {t.name for t in tree.find_all(exp.Table)} - cte_names
    unknown = sorted(used - known_tables)
    record("known_tables", not unknown, f"Tables: {', '.join(sorted(used))}" if not unknown
           else f"Unknown tables: {', '.join(unknown)}")

    # 4. Date filter on big partitioned tables. Some WHERE clause must COMPARE
    # the date column with a value (>=, <, BETWEEN, =, ...). Merely
    # mentioning it is not enough: "txn_date IS NOT NULL" is true for every
    # row, so it filters nothing and Postgres would still read all 60 months.
    comparisons = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between, exp.In)
    for table, date_column in date_filter_required.items():
        if table in used:
            filtered = any(
                any(col.name == date_column for col in cmp.find_all(exp.Column))
                for where in tree.find_all(exp.Where) for cmp in where.find_all(*comparisons)
            )
            record("date_filter", filtered, f"{table} filtered by a range on {date_column}" if filtered
                   else f"{table} must be filtered by a date range on {date_column} (e.g. >= and <)")
    return results


def tables_in(sql: str) -> list[str]:
    """The real tables a query reads (not its WITH names). Empty if it does not parse."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return []
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return sorted({t.name for t in tree.find_all(exp.Table)} - cte_names)