"""
adapters/postgres.py
====================

WHAT THIS FILE IS FOR
---------------------
The ONLY code in the project that talks to the fund database (mf_data
locally, the client's database in production). Everything else calls one
of these four functions and never writes engine-specific SQL itself:

    read_catalog()        -> tables, columns, types, keys, row counts
    read_column_stats()   -> distinct counts, null rates, common values
    plan_estimate(sql)    -> how expensive Postgres expects a query to be
    run_readonly(sql)     -> run a SELECT safely: read-only, time-limited, row-capped

WHY ONE ADAPTER
---------------
This is tradeoff 2. The client's database might one day be SQL Server or
Snowflake instead of Postgres. If only this file knows it is talking to
Postgres, supporting another engine means writing one new adapter file
with the same four functions, and nothing else in the pipeline changes.

WHERE ITS DATA COMES FROM
-------------------------
Postgres keeps a description of itself in "system catalogs": pg_class
(tables), pg_attribute (columns), pg_constraint (keys) and pg_stats
(statistics gathered by ANALYZE). Reading those is fast and never touches
the actual data rows, which matters on a 100 GB database.
"""

# dataclass: small classes that only hold values. field: sets a default (like an empty list).
from dataclasses import dataclass, field

# psycopg: the PostgreSQL driver.
import psycopg


# @dataclass writes the __init__ for us, so we can write Column(name, data_type, nullable).
@dataclass
# One column of a table.
class Column:
    # The column's name.
    name: str
    data_type: str                 # e.g. "integer", "text", "numeric(18,2)"
    # True if the column may be empty (NULL).
    nullable: bool


# @dataclass: ForeignKey(from_table, from_column, to_table, to_column).
@dataclass
# One declared link: from_table.from_column points to to_table.to_column.
class ForeignKey:
    # The table that holds the pointer column.
    from_table: str
    # The pointer column itself.
    from_column: str
    # The table it points to.
    to_table: str
    # The column it points to (usually that table's primary key).
    to_column: str


# @dataclass: Table(name, row_estimate, ...); the lists start empty.
@dataclass
# One table, with its columns and keys.
class Table:
    # The table's name.
    name: str
    row_estimate: int              # from Postgres statistics, not an exact count
    # default_factory=list gives every Table its OWN new empty list.
    columns: list[Column] = field(default_factory=list)
    # The primary key's column names (may be several).
    primary_key: list[str] = field(default_factory=list)
    unique_columns: list[str] = field(default_factory=list)   # single-column UNIQUE only


# @dataclass: Catalog(tables, foreign_keys).
@dataclass
# Everything read_catalog() found: all tables plus all declared foreign keys.
class Catalog:
    # Table name -> Table.
    tables: dict[str, Table]
    # Every declared single-column foreign key.
    foreign_keys: list[ForeignKey]


# @dataclass: ColumnStats(null_fraction, distinct_values, common_values).
@dataclass
# Statistics Postgres keeps about one column.
class ColumnStats:
    null_fraction: float           # 0.0 = never empty, 1.0 = always empty
    distinct_values: float         # estimated number of different values
    common_values: list[str]       # the most frequent values, as text


# Create one with:  PostgresAdapter(os.environ["DATA_DB_URL"])
class PostgresAdapter:
    """Talks to one Postgres database, identified by its connection URL."""

    # __init__ runs when the object is created. "public" is Postgres's default schema.
    def __init__(self, database_url: str, schema: str = "public"):
        # Remember the connection address ...
        self.database_url = database_url
        # ... and which schema (namespace) to read.
        self.schema = schema

    # A leading "_" means "only for use inside this class".
    def _connect(self):
        # autocommit: each query stands alone, so one failed query never
        # blocks the next one (the lesson from the Stage 1 tests).
        return psycopg.connect(self.database_url, autocommit=True)

    # -----------------------------------------------------------------------
    # 1. read_catalog
    # -----------------------------------------------------------------------
    # Read every table, column, key and foreign key from the system catalogs.
    def read_catalog(self) -> Catalog:
        # Open a connection; it closes automatically at the end of the block.
        with self._connect() as conn:
            # Main tables only. relkind 'r' = ordinary table, 'p' = partitioned
            # table. relispartition = true marks the 60 monthly pieces of
            # transactions, which are storage details, not tables to describe.
            # conn.execute(sql, params) runs the SQL; %s is filled in safely with self.schema.
            table_rows = conn.execute(
                # The SQL text (between the triple quotes): every main table in the schema, with its kind.
                """
                SELECT c.relname, c.relkind
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
                ORDER BY c.relname
                """,
                # The value for %s: the schema name (a 1-item tuple needs the trailing comma).
                (self.schema,),
            # .fetchall() returns every result row as a list of tuples.
            ).fetchall()

            # Table name -> Table object, filled in below.
            tables = {}
            # Each row is (table name, relkind).
            for name, kind in table_rows:
                # reltuples is Postgres's row-count estimate, kept up to date
                # by ANALYZE. A partitioned table stores no rows itself, so we
                # add up its pieces instead.
                # Partitioned table: add up the row estimates of all its pieces (pg_inherits).
                if kind == "p":
                    # Sum the row estimates of every monthly piece (pg_inherits links pieces to their parent).
                    rows = conn.execute(
                        # SQL: add up the pieces' estimates (coalesce turns "no pieces" into 0).
                        """SELECT coalesce(sum(greatest(c.reltuples, 0)), 0) FROM pg_inherits i
                           JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = %s::regclass""",
                        # The value for %s: the table name.
                        (name,),
                    ).fetchone()[0]
                # Ordinary table: read its own estimate.
                else:
                    # The table's own row estimate (never below 0).
                    rows = conn.execute(
                        # SQL and its value (the table name).
                        "SELECT greatest(reltuples, 0) FROM pg_class WHERE oid = %s::regclass", (name,)
                    ).fetchone()[0]
                # Create the Table object (columns and keys are added below).
                tables[name] = Table(name=name, row_estimate=int(rows))

            # Columns, in table order. format_type() gives the full type,
            # e.g. "numeric(18,2)" rather than just "numeric".
            for table, column, data_type, not_null in conn.execute(
                # SQL: every column of every main table, with its full type and NOT NULL flag, in column order.
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
                  AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
                """,
                # The value for %s: the schema name.
                (self.schema,),
            ).fetchall():
                # "not not_null" turns "NOT NULL is set" into "nullable".
                tables[table].columns.append(Column(column, data_type, not not_null))

            # Primary keys and unique constraints. contype 'p' = primary key,
            # 'u' = unique. conkey lists the column numbers they cover.
            for table, kind, columns in conn.execute(
                # SQL: every primary key / unique constraint, with its column names in key order.
                """
                SELECT c.relname, k.contype,
                       array_agg(a.attname ORDER BY array_position(k.conkey, a.attnum))
                FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)
                WHERE n.nspname = %s AND k.contype IN ('p', 'u') AND NOT c.relispartition
                GROUP BY c.relname, k.oid, k.contype
                """,
                # The value for %s: the schema name.
                (self.schema,),
            ).fetchall():
                # Skip constraints on tables we are not describing.
                if table not in tables:
                    # Skip to the next constraint.
                    continue
                # A primary key: store all its columns.
                if kind == "p":
                    # Store the primary key's column names.
                    tables[table].primary_key = list(columns)
                # A single-column UNIQUE constraint: remember the column.
                elif len(columns) == 1:
                    # Store the one UNIQUE column.
                    tables[table].unique_columns.append(columns[0])

            # Declared foreign keys (contype 'f'). Single-column ones only,
            # which covers every link in this schema.
            # Build one ForeignKey per result row (*row spreads the 4 values into the 4 fields).
            foreign_keys = [
                # For each result row, make a ForeignKey ...
                ForeignKey(*row)
                # ... from this query's results:
                for row in conn.execute(
                    # SQL: single-column foreign keys: source table/column -> target table/column.
                    """
                    SELECT src.relname, sa.attname, dst.relname, da.attname
                    FROM pg_constraint k
                    JOIN pg_class src ON src.oid = k.conrelid
                    JOIN pg_class dst ON dst.oid = k.confrelid
                    JOIN pg_namespace n ON n.oid = src.relnamespace
                    JOIN pg_attribute sa ON sa.attrelid = k.conrelid AND sa.attnum = k.conkey[1]
                    JOIN pg_attribute da ON da.attrelid = k.confrelid AND da.attnum = k.confkey[1]
                    WHERE n.nspname = %s AND k.contype = 'f' AND cardinality(k.conkey) = 1
                      AND NOT src.relispartition
                    ORDER BY 1, 2
                    """,
                    # The value for %s: the schema name.
                    (self.schema,),
                ).fetchall()
            ]
        # Hand back everything found.
        return Catalog(tables=tables, foreign_keys=foreign_keys)

    # -----------------------------------------------------------------------
    # 2. read_column_stats
    # -----------------------------------------------------------------------
    def read_column_stats(self, catalog: Catalog) -> dict[tuple[str, str], ColumnStats]:
        """
        Statistics for every column, keyed by (table, column).

        pg_stats is filled by ANALYZE (the loader runs it). n_distinct has a
        quirk: a positive number is a count ("40 different values"), while a
        negative number is a fraction of the row count ("-1" means every row
        is different). We turn both into an estimated count.
        """
        # (table, column) -> ColumnStats, filled in below.
        stats = {}
        # Open a connection to the fund database.
        with self._connect() as conn:
            # One row per column from pg_stats: its table, name, empty rate, distinct count and common values.
            for table, column, null_frac, n_distinct, common in conn.execute(
                # SQL: column statistics from pg_stats, whole-table rows first.
                """
                SELECT tablename, attname, null_frac, n_distinct, most_common_vals::text
                FROM pg_stats WHERE schemaname = %s
                ORDER BY inherited DESC   -- for partitioned tables, prefer whole-table stats
                """,
                # The value for %s: the schema name.
                (self.schema,),
            ).fetchall():
                # Skip unknown tables, and duplicates (the first row - whole-table stats - wins).
                if table not in catalog.tables or (table, column) in stats:
                    # Skip this row.
                    continue
                # The table's estimated row count.
                rows = catalog.tables[table].row_estimate
                # Turn a negative fraction into a count (see the docstring above).
                distinct = n_distinct if n_distinct >= 0 else -n_distinct * rows
                # most_common_vals arrives as Postgres array text like
                # {Equity,Debt,"Solution Oriented"}. We strip the braces and
                # quotes to get a plain list.
                # Empty list when there are no common values.
                values = []
                # Only if Postgres recorded common values for this column.
                if common:
                    # Remove { }, split on commas, remove quotes around each value.
                    values = [v.strip('"') for v in common.strip("{}").split(",") if v]
                # Save the statistics under the (table, column) key.
                stats[(table, column)] = ColumnStats(float(null_frac), float(distinct), values)
        # Hand back all statistics.
        return stats

    # -----------------------------------------------------------------------
    # 3. plan_estimate
    # -----------------------------------------------------------------------
    def plan_estimate(self, sql: str) -> dict:
        """
        Ask Postgres how it would run a query, WITHOUT running it.
        EXPLAIN returns the planner's estimate of cost and row count.
        Used in Stage 7 to refuse queries that would be too expensive.
        """
        # Open a connection, ask for the plan, and close the connection.
        with self._connect() as conn:
            # EXPLAIN returns one row whose single value is a JSON list; [0]["Plan"] is the top plan step.
            plan = conn.execute("EXPLAIN (FORMAT JSON) " + sql).fetchone()[0][0]["Plan"]
        # Return the planner's cost estimate and expected row count.
        return {"total_cost": plan["Total Cost"], "rows": plan["Plan Rows"]}

    # -----------------------------------------------------------------------
    # 4. run_readonly
    # -----------------------------------------------------------------------
    def run_readonly(self, sql: str, params=None, timeout_ms: int = 15000,
                     # Defaults: 15 seconds, 5,000 rows. Returns (column names, rows).
                     row_cap: int = 5000) -> tuple[list[str], list[tuple]]:
        """
        Run a query that can only read, never change, the database.

        Three protections, all enforced by Postgres itself:
          - READ ONLY transaction: any INSERT/UPDATE/DELETE/DROP is refused.
          - statement_timeout: the query is cancelled after timeout_ms.
          - row cap: at most row_cap rows are fetched back.
        Returns (column names, rows).
        """
        # A normal (not autocommit) connection, so the SET commands below apply to one transaction.
        with psycopg.connect(self.database_url) as conn:
            # Postgres itself now refuses any write in this transaction.
            conn.execute("SET TRANSACTION READ ONLY")
            # Cancel the query if it runs longer than timeout_ms (LOCAL = this transaction only).
            conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            # Run the query; params fills any %s placeholders safely.
            cur = conn.execute(sql, params)
            # cur.description describes the result columns; take their names.
            columns = [d.name for d in cur.description]
            # Fetch at most row_cap rows.
            rows = cur.fetchmany(row_cap)
            conn.rollback()     # nothing to save; end the read-only transaction cleanly
        # Return (column names, rows) as a tuple.
        return columns, rows

    # -----------------------------------------------------------------------
    # Helpers used by the metadata pipeline
    # -----------------------------------------------------------------------
    def sample_values(self, table: str, column: str, limit: int) -> list[str]:
        """A few real values from a column, for columns with no common values in pg_stats."""
        # Open a connection to the fund database.
        with self._connect() as conn:
            # Up to "limit" different non-empty values, as text. Names are double-quoted for safety.
            rows = conn.execute(
                # SQL: distinct non-empty values as text; names in double quotes; LIMIT is the %s.
                f'SELECT DISTINCT "{column}"::text FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT %s',
                # The value for %s: how many values.
                (limit,),
            ).fetchall()
        # Each row is a 1-item tuple; take the value out.
        return [r[0] for r in rows]

    # Used by metadata/links.py to test whether two columns hold the same values.
    def value_overlap(self, from_table: str, from_column: str, to_table: str, to_column: str) -> tuple[float, int]:
        """
        Share of the distinct non-empty values in from_column that also
        appear in to_column. 1.0 means every value matches.
        Returns (share, number of distinct values checked).

        NULLs are left out on purpose: in transactions.arn_code, NULL means
        "no distributor", not a broken link, so counting it would make a real
        link look weaker than it is.
        """
        # Open a connection to the fund database.
        with self._connect() as conn:
            # count(t.v) counts only rows where a match was found (t.v is
            # empty when the LEFT JOIN found nothing); count(*) counts all.
            # The query returns one row: (matched count, total count).
            matched, total = conn.execute(
                # SQL: distinct values on each side, LEFT JOINed on equal text.
                f"""
                SELECT count(t.v), count(*)
                FROM (SELECT DISTINCT "{from_column}"::text AS v FROM "{from_table}"
                      WHERE "{from_column}" IS NOT NULL) s
                LEFT JOIN (SELECT DISTINCT "{to_column}"::text AS v FROM "{to_table}") t ON t.v = s.v
                """
            ).fetchone()
        # The share of matching values (0.0 if there were none), and how many were checked.
        return (matched / total if total else 0.0), total