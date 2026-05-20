"""Generate a PostgreSQL schema.sql from the SQLAlchemy models in db.py.

Run with:
    uv run scripts/export_schema.py [output_path]

Default output: ./schema.sql
"""

import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from data_commons_search.db import Base


def render_schema_sql() -> str:
    """Render a CREATE TABLE statement per ORM table, in dependency order."""
    dialect = postgresql.dialect()
    statements = []
    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=dialect)).strip()
        sql = "\n".join(line.rstrip() for line in sql.splitlines())
        statements.append(sql + ";")
    header = (
        "-- AUTO-GENERATED FILE. DO NOT EDIT MANUALLY.\n"
        "-- Generated from https://github.com/EOSC-Data-Commons/data-commons-search/blob/main/src/data_commons_search/db.py\n"
    )
    return header + "\n\n".join(statements) + "\n"


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("schema.sql")
    out_path.write_text(render_schema_sql())
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
