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

from dataclasses import dataclass, field

import psycopg


@dataclass
class Column:
    name: str
    data_type: str                 # e.g. "integer", "text", "numeric(18,2)"
    nullable: bool


@dataclass
class ForeignKey:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class Table:
    name: str
    row_estimate: int              # from Postgres statistics, not an exact count
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    unique_columns: list[str] = field(default_factory=list)   # single-column UNIQUE only


@dataclass
class Catalog:
    tables: dict[str, Table]
    foreign_keys: list[ForeignKey]


@dataclass
class ColumnStats:
    null_fraction: float           # 0.0 = never empty, 1.0 = always empty
    distinct_values: float         # estimated number of different values
    common_values: list[str]       # the most frequent values, as text


class PostgresAdapter:
    """Talks to one Postgres database, identified by its connection URL."""

    def __init__(self, database_url: str, schema: str = "public"):
        self.database_url = database_url
        self.schema = schema

    def _connect(self):
        # autocommit: each query stands alone, so one failed query never
        # blocks the next one (the lesson from the Stage 1 tests).
        return psycopg.connect(self.database_url, autocommit=True)

    # -----------------------------------------------------------------------
    # 1. read_catalog
    # -----------------------------------------------------------------------
    def read_catalog(self) -> Catalog:
        with self._connect() as conn:
            # Main tables only. relkind 'r' = ordinary table, 'p' = partitioned
            # table. relispartition = true marks the 60 monthly pieces of
            # transactions, which are storage details, not tables to describe.
            table_rows = conn.execute(
                """
                SELECT c.relname, c.relkind
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
                ORDER BY c.relname
                """,
                (self.schema,),
            ).fetchall()

            tables = {}
            for name, kind in table_rows:
                # reltuples is Postgres's row-count estimate, kept up to date
                # by ANALYZE. A partitioned table stores no rows itself, so we
                # add up its pieces instead.
                if kind == "p":
                    rows = conn.execute(
                        """SELECT coalesce(sum(greatest(c.reltuples, 0)), 0) FROM pg_inherits i
                           JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = %s::regclass""",
                        (name,),
                    ).fetchone()[0]
                else:
                    rows = conn.execute(
                        "SELECT greatest(reltuples, 0) FROM pg_class WHERE oid = %s::regclass", (name,)
                    ).fetchone()[0]
                tables[name] = Table(name=name, row_estimate=int(rows))

            # Columns, in table order. format_type() gives the full type,
            # e.g. "numeric(18,2)" rather than just "numeric".
            for table, column, data_type, not_null in conn.execute(
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
                  AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY c.relname, a.attnum
                """,
                (self.schema,),
            ).fetchall():
                tables[table].columns.append(Column(column, data_type, not not_null))

            # Primary keys and unique constraints. contype 'p' = primary key,
            # 'u' = unique. conkey lists the column numbers they cover.
            for table, kind, columns in conn.execute(
                """
                SELECT c.relname, k.contype,
                       array_agg(a.attname ORDER BY array_position(k.conkey, a.attnum))
                FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)
                WHERE n.nspname = %s AND k.contype IN ('p', 'u') AND NOT c.relispartition
                GROUP BY c.relname, k.oid, k.contype
                """,
                (self.schema,),
            ).fetchall():
                if table not in tables:
                    continue
                if kind == "p":
                    tables[table].primary_key = list(columns)
                elif len(columns) == 1:
                    tables[table].unique_columns.append(columns[0])

            # Declared foreign keys (contype 'f'). Single-column ones only,
            # which covers every link in this schema.
            foreign_keys = [
                ForeignKey(*row)
                for row in conn.execute(
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
                    (self.schema,),
                ).fetchall()
            ]
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
        stats = {}
        with self._connect() as conn:
            for table, column, null_frac, n_distinct, common in conn.execute(
                """
                SELECT tablename, attname, null_frac, n_distinct, most_common_vals::text
                FROM pg_stats WHERE schemaname = %s
                ORDER BY inherited DESC   -- for partitioned tables, prefer whole-table stats
                """,
                (self.schema,),
            ).fetchall():
                if table not in catalog.tables or (table, column) in stats:
                    continue
                rows = catalog.tables[table].row_estimate
                distinct = n_distinct if n_distinct >= 0 else -n_distinct * rows
                # most_common_vals arrives as Postgres array text like
                # {Equity,Debt,"Solution Oriented"}. We strip the braces and
                # quotes to get a plain list.
                values = []
                if common:
                    values = [v.strip('"') for v in common.strip("{}").split(",") if v]
                stats[(table, column)] = ColumnStats(float(null_frac), float(distinct), values)
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
        with self._connect() as conn:
            plan = conn.execute("EXPLAIN (FORMAT JSON) " + sql).fetchone()[0][0]["Plan"]
        return {"total_cost": plan["Total Cost"], "rows": plan["Plan Rows"]}

    # -----------------------------------------------------------------------
    # 4. run_readonly
    # -----------------------------------------------------------------------
    def run_readonly(self, sql: str, params=None, timeout_ms: int = 15000,
                     row_cap: int = 5000) -> tuple[list[str], list[tuple]]:
        """
        Run a query that can only read, never change, the database.

        Three protections, all enforced by Postgres itself:
          - READ ONLY transaction: any INSERT/UPDATE/DELETE/DROP is refused.
          - statement_timeout: the query is cancelled after timeout_ms.
          - row cap: at most row_cap rows are fetched back.
        Returns (column names, rows).
        """
        with psycopg.connect(self.database_url) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur = conn.execute(sql, params)
            columns = [d.name for d in cur.description]
            rows = cur.fetchmany(row_cap)
            conn.rollback()     # nothing to save; end the read-only transaction cleanly
        return columns, rows

    # -----------------------------------------------------------------------
    # Helpers used by the metadata pipeline
    # -----------------------------------------------------------------------
    def sample_values(self, table: str, column: str, limit: int) -> list[str]:
        """A few real values from a column, for columns with no common values in pg_stats."""
        with self._connect() as conn:
            rows = conn.execute(
                f'SELECT DISTINCT "{column}"::text FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT %s',
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]

    def value_overlap(self, from_table: str, from_column: str, to_table: str, to_column: str) -> tuple[float, int]:
        """
        Share of the distinct non-empty values in from_column that also
        appear in to_column. 1.0 means every value matches.
        Returns (share, number of distinct values checked).

        NULLs are left out on purpose: in transactions.arn_code, NULL means
        "no distributor", not a broken link, so counting it would make a real
        link look weaker than it is.
        """
        with self._connect() as conn:
            # count(t.v) counts only rows where a match was found (t.v is
            # empty when the LEFT JOIN found nothing); count(*) counts all.
            matched, total = conn.execute(
                f"""
                SELECT count(t.v), count(*)
                FROM (SELECT DISTINCT "{from_column}"::text AS v FROM "{from_table}"
                      WHERE "{from_column}" IS NOT NULL) s
                LEFT JOIN (SELECT DISTINCT "{to_column}"::text AS v FROM "{to_table}") t ON t.v = s.v
                """
            ).fetchone()
        return (matched / total if total else 0.0), total