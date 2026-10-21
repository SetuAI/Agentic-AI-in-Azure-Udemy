"""
metadata/links.py
=================

WHAT THIS FILE IS FOR
---------------------
Finds every link between tables in the fund database: the ones the
database declares, and the ones nobody declared. This is tradeoff 4.

    SOURCE 1  declared       Foreign keys from the catalog. Certain.
                             Saved as 'approved' straight away.

    SOURCE 2  same_name      A column with the same name as another table's
                             single-column primary key, e.g.
                             scheme_aum_monthly.scheme_id -> schemes.scheme_id.
                             The name is only a hint; the values are checked.

    SOURCE 3  value_overlap  A text column whose values almost all appear in
                             another table's unique text column, whatever the
                             names, e.g. transactions.arn_code -> distributors.arn_no.

    (SOURCE 4, query history from pg_stat_statements, is left out here:
    the local database has no query history to learn from. It is added
    when running against the client's database.)

Links from sources 2 and 3 are saved as 'proposed'. A human approves them
in review.py before any of them reaches the graph.

WHAT THIS FILE DOES NOT READ
----------------------------
evals/answer_key/hidden_links.yaml. The answer key is used only by the test
that scores this file's results, never by this file itself.
"""

from dataclasses import dataclass

from adapters.postgres import Catalog, PostgresAdapter

# Types that can hold matching values. A text column is never compared with a
# number column, and vice versa.
INTEGER_TYPES = {"integer", "bigint", "smallint"}


def _type_family(data_type: str) -> str:
    if data_type in INTEGER_TYPES:
        return "integer"
    if data_type == "text" or data_type.startswith(("character", "varchar")):
        return "text"
    return data_type


@dataclass
class Link:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    source: str
    confidence: float
    evidence: str


def discover(adapter: PostgresAdapter, catalog: Catalog, stats: dict, settings: dict) -> list[Link]:
    min_overlap = settings["min_overlap"]
    min_distinct = settings["min_distinct_for_value_match"]
    links: list[Link] = []

    # ---- SOURCE 1: declared foreign keys -------------------------------
    declared = set()
    for fk in catalog.foreign_keys:
        links.append(Link(fk.from_table, fk.from_column, fk.to_table, fk.to_column,
                          "declared", 1.0, "Declared as a foreign key in the database."))
        declared.add((fk.from_table, fk.from_column))

    # Columns that other columns could point TO: a single-column primary key,
    # or a single-column UNIQUE constraint. A link always points at something
    # that identifies exactly one row.
    targets = []
    for table in catalog.tables.values():
        types = {c.name: c.data_type for c in table.columns}
        if len(table.primary_key) == 1:
            targets.append((table.name, table.primary_key[0], types[table.primary_key[0]]))
        for column in table.unique_columns:
            targets.append((table.name, column, types[column]))

    # ---- SOURCES 2 and 3: undeclared links -----------------------------
    for table in catalog.tables.values():
        for column in table.columns:
            # Skip columns already covered by a declared foreign key.
            if (table.name, column.name) in declared:
                continue
            # Skip a table's own single-column primary key: it is the thing
            # others point to, not a pointer itself.
            if table.primary_key == [column.name]:
                continue

            for to_table, to_column, to_type in targets:
                if to_table == table.name or _type_family(to_type) != _type_family(column.data_type):
                    continue

                if column.name == to_column:
                    source = "same_name"
                else:
                    # Value matching is only worth trying on text columns with
                    # enough different values; small lists match by coincidence.
                    distinct = stats.get((table.name, column.name))
                    if _type_family(column.data_type) != "text" or distinct is None \
                            or distinct.distinct_values < min_distinct:
                        continue
                    source = "value_overlap"

                share, checked = adapter.value_overlap(table.name, column.name, to_table, to_column)
                if share >= min_overlap:
                    links.append(Link(
                        table.name, column.name, to_table, to_column, source, round(share, 3),
                        f"{share:.1%} of {checked:,} distinct values in {table.name}.{column.name} "
                        f"exist in {to_table}.{to_column}"
                        + (" (same column name)." if source == "same_name" else "."),
                    ))
    return links