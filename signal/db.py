"""Small DuckDB helpers shared by Signal's collector and importers."""

from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "signal.duckdb"


def connect(path: Path = DEFAULT_DB, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    # Views are cheap derived objects; recreating them lets schema improvements
    # take effect when an existing local database is opened.
    for view in ("speedtest_dns_windows", "pihole_client_monthly", "pihole_domain_monthly", "pihole_daily", "pihole_queries_enriched", "speedtest_results"):
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    conn.execute((ROOT / "schema.sql").read_text())
    conn.executemany("INSERT OR IGNORE INTO pihole_status_codes VALUES (?,?,?)", [
        (0, "Unknown", "unknown"),
        (1, "Blocked · gravity", "blocked"),
        (2, "Allowed · forwarded", "allowed_forwarded"),
        (3, "Allowed · cache", "allowed_cached"),
        (4, "Blocked · regex", "blocked"),
        (5, "Blocked · exact", "blocked"),
        (6, "Blocked · upstream IP", "blocked"),
        (7, "Blocked · upstream null", "blocked"),
        (8, "Blocked · upstream NXDOMAIN", "blocked"),
        (9, "Blocked · deep CNAME gravity", "blocked"),
        (10, "Blocked · deep CNAME regex", "blocked"),
        (11, "Blocked · deep CNAME exact", "blocked"),
        (12, "Allowed · retried", "allowed_other"),
        (13, "Allowed · retried ignored", "allowed_other"),
        (14, "Allowed · already forwarded", "allowed_forwarded"),
        (15, "Blocked · database busy", "blocked"),
        (16, "Blocked · special domain", "blocked"),
        (17, "Allowed · stale cache", "allowed_cached"),
        (18, "Blocked · upstream EDE", "blocked"),
    ])
    conn.execute("INSERT OR IGNORE INTO source_metadata VALUES ('signal_schema', 'signal/schema.sql', NULL, NULL, now(), 'Canonical Signal schema')")
