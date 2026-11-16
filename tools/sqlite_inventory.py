"""Read-only inventory of existing SEKA WEB SQLite data before PostgreSQL migration.
Never prints passwords, salts, tokens, file keys or personal data.
Usage: python sqlite_inventory.py /var/lib/seka-web-dev/core.sqlite
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

EXPECTED = ("users", "roles", "sessions", "modules", "jobs", "events",
            "files", "audit", "notifications")

def inspect(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(set(EXPECTED) - tables)
        if missing:
            raise RuntimeError("Missing tables: " + ", ".join(missing))
        counts = {name: conn.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                  for name in EXPECTED}
        admin_count = conn.execute("SELECT COUNT(*) FROM roles WHERE role='admin'").fetchone()[0]
        usernames = [row[0] for row in conn.execute("SELECT username FROM users ORDER BY username")]
        digest = hashlib.sha256(json.dumps(usernames, ensure_ascii=False).encode()).hexdigest()
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        return {"tables": counts, "admin_role_count": admin_count,
                "username_set_sha256": digest, "integrity": integrity,
                "ready_for_migration": integrity == "ok" and admin_count > 0}
    finally:
        conn.rollback()
        conn.close()

if __name__ == "__main__":
    try:
        print(json.dumps(inspect(sys.argv[1]), ensure_ascii=False, indent=2))
    except (IndexError, OSError, sqlite3.Error, RuntimeError) as exc:
        print("Inventory failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
