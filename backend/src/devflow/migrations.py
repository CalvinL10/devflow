"""Transactional upgrades. Operators must stop and back up data/workspaces first."""

from __future__ import annotations

import sqlite3

VERSION = 2


def _statements(schema: str):
    pending = ""
    for line in schema.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete database schema")


def _rebuild(connection: sqlite3.Connection, table: str, definition: str, schema: str) -> None:
    # SQLite's table-rebuild procedure: never rename the old parent first, which
    # would redirect child foreign keys. Preserve existing explicit indexes/triggers.
    objects = connection.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    create = next(sql for sql in _statements(schema) if f"CREATE TABLE IF NOT EXISTS {table} (" in sql)
    connection.execute(create.replace(f"{table} (", f"{table}_migration (", 1))
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    names = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    values = [f'"{name}"' for name in columns]
    if table == "runs" and "'CANCELLED'" in definition:
        values[columns.index("status")] = "CASE status WHEN 'CANCELLED' THEN 'CANCELED' ELSE status END"
    connection.execute(f'INSERT INTO "{table}_migration" ({names}) SELECT {", ".join(values)} FROM "{table}"')
    connection.execute(f'DROP TABLE "{table}"')
    connection.execute(f'ALTER TABLE "{table}_migration" RENAME TO "{table}"')
    for row in objects:
        connection.execute(row[0])


def migrate(connection: sqlite3.Connection, schema: str) -> None:
    # Foreign keys are disabled only on this private startup connection during a
    # transactionally checked table rebuild, as required by SQLite; normal runtime
    # connections always enforce them. Any failure rolls back the entire upgrade.
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > VERSION:
            raise RuntimeError("database is newer than this application; restore a matching backup")
        if version < 2:
            definitions = dict(connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('runs', 'decisions')"
            ).fetchall())
            for table, required in (("runs", "'CANCELED'"), ("decisions", "'cancel'")):
                if table in definitions and required not in definitions[table]:
                    _rebuild(connection, table, definitions[table], schema)
            if "decisions" in definitions:
                connection.execute("UPDATE decisions SET result_status='CANCELED' WHERE result_status='CANCELLED'")
        for statement in _statements(schema):
            connection.execute(statement)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("database migration found invalid references; restore or repair a backup")
        connection.execute(f"PRAGMA user_version = {VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
