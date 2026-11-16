"""SEKA WEB SQLite -> PostgreSQL migration manifest and read-only source validation.
This tool does not modify PostgreSQL, SQLite, or reveal credentials.
"""
import json
import sqlite3
import sys
from pathlib import Path

TABLES = {
    "users": ("id", "username", "salt", "password_hash", "active"),
    "roles": ("username", "role"),
    "sessions": ("token_hash", "username", "expires"),
    "modules": ("id", "version", "enabled", "error"),
    "jobs": ("id", "module_id", "kind", "payload", "status", "progress", "owner", "created", "idempotency_key"),
    "events": ("id", "module_id", "type", "payload", "actor", "created"),
    "files": ("id", "name", "storage_key", "sha256", "size", "owner", "module_id", "created"),
    "audit": ("id", "actor", "action", "subject", "created"),
    "notifications": ("id", "recipient", "message", "actor", "created", "read_at"),
}

def validate_source(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        actual = {}
        for table, expected in TABLES.items():
            columns = tuple(row[1] for row in conn.execute('PRAGMA table_info("' + table + '")'))
            if columns != expected:
                raise RuntimeError("Unexpected source schema: " + table)
            actual[table] = conn.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        if conn.execute("SELECT count(*) FROM roles WHERE role='admin'").fetchone()[0] < 1:
            raise RuntimeError("No administrator role")
        return {"schema_valid": True, "counts": actual, "migration_allowed": False,
                "reason": "PostgreSQL destination schema must be validated and approved by SQL Manager"}

if __name__ == "__main__":
    try:
        print(json.dumps(validate_source(sys.argv[1]), indent=2))
    except (IndexError, RuntimeError, sqlite3.Error, OSError) as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        sys.exit(1)
