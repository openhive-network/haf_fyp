"""The shipped CREATE TABLE and CREATE INDEX statements of db/create_tables.sql, by table.

The integration tests run in lanes that cannot load the whole file (the fastapi lane has
no pgvector), and a table written out again in a test is a copy free to drift from the
one the install makes. This returns the file's own statements for the tables a test
names, which it runs inside its rolled-back transaction after dropping whatever narrower
stand-in the lane's setup made (tests/ci_fastapi_setup.sql).
"""

from __future__ import annotations

import os
import re

CREATE_TABLES = os.path.join(os.path.dirname(__file__), "..", "..", "db", "create_tables.sql")


def ddl(*tables: str) -> str:
    """DROP, CREATE TABLE and every CREATE INDEX of each table, in the file's order.

    :raises LookupError: a table the file does not create.
    """
    with open(CREATE_TABLES, encoding="utf-8") as f:
        text = f.read()
    statements = []
    for table in tables:
        create = re.search(
            rf"^CREATE TABLE IF NOT EXISTS haf_fyp_app\.{table} \(.*?^\);", text, re.S | re.M)
        if create is None:
            raise LookupError(f"db/create_tables.sql creates no haf_fyp_app.{table}")
        statements.append(f"DROP TABLE IF EXISTS haf_fyp_app.{table} CASCADE;")
        statements.append(create.group(0))
        statements += re.findall(
            rf"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+\s+ON haf_fyp_app\.{table}\b[^;]*;",
            text, re.M)
    return "\n".join(statements)


def function(name: str) -> str:
    """The CREATE OR REPLACE FUNCTION statement of haf_fyp_app.<name> in db/create_tables.sql.

    :raises LookupError: a function the file does not define.
    """
    with open(CREATE_TABLES, encoding="utf-8") as f:
        text = f.read()
    found = re.search(
        rf"^CREATE OR REPLACE FUNCTION haf_fyp_app\.{name}\(.*?^\$\$;", text, re.S | re.M)
    if found is None:
        raise LookupError(f"db/create_tables.sql defines no haf_fyp_app.{name}")
    return found.group(0)
