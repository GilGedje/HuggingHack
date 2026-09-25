"""Copy an existing SQLite installation into PostgreSQL.

Run it once with PostgreSQL up but before HuggingHack starts on it, because the
target must not have any accounts yet:

    docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d postgres
    docker compose -f docker-compose.yml -f docker-compose.postgres.yml run --rm \
        hugginghack python -m app.migrate_sqlite

The source file is copied and upgraded to the current schema first, so the
original SQLite database is never modified. The target must be empty.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .config import settings
from .database import Database


# Parents before children so foreign keys are satisfied while copying.
TABLES = (
    "users",
    "sessions",
    "api_tokens",
    "collections",
    "saved_models",
    "collection_items",
    "organizations",
    "organization_members",
    "owned_repositories",
    "downloads",
    "runtime_jobs",
    "local_models",
    "repo_commits",
    "text_blobs",
    "file_digests",
    "model_hardware",
    "model_listing",
    "storage_moves",
    "revision_aliases",
    "storage_grants",
    "config_revisions",
)


def _columns(database: Database, table: str) -> list[str]:
    with database.connect() as connection:
        return sorted(database._column_names(connection, table))


def migrate(source_path: Path, target_url: str) -> dict[str, int]:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")
    if not target_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("DATABASE_URL must point at PostgreSQL.")
    target = Database(target_url)
    try:
        return _copy(source_path, target)
    finally:
        target.close()


def _copy(source_path: Path, target: Database) -> dict[str, int]:
    target.initialize()
    if target.count_users():
        raise ValueError("The PostgreSQL database already has accounts; refusing to overwrite it.")

    copied: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as workspace:
        snapshot = Path(workspace) / "source.sqlite3"
        # The backup API gives a consistent copy even while the app is running.
        with sqlite3.connect(source_path) as original, sqlite3.connect(snapshot) as copy:
            original.backup(copy)
        source = Database(snapshot)
        source.initialize()
        for table in TABLES:
            columns = [
                column
                for column in _columns(source, table)
                if column in set(_columns(target, table))
            ]
            if not columns:
                continue
            with source.connect() as connection:
                rows = connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table}"
                ).fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                with target.connect() as connection:
                    connection.executemany(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        [tuple(row[column] for column in columns) for row in rows],
                    )
            copied[table] = len(rows)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=settings.database_path,
        help="SQLite file to copy (default: the file in DATA_DIR).",
    )
    parser.add_argument(
        "--target",
        default=settings.database_url or "",
        help="PostgreSQL URL (default: DATABASE_URL).",
    )
    args = parser.parse_args()
    try:
        copied = migrate(args.source, args.target)
    except (FileNotFoundError, ValueError) as error:
        print(f"Migration stopped: {error}", file=sys.stderr)
        sys.exit(1)
    for table, count in copied.items():
        print(f"{table}: {count}")
    retired = args.source.with_suffix(args.source.suffix + ".migrated")
    shutil.copy2(args.source, retired)
    print(f"Done. A copy of the SQLite file was kept at {retired}.")


if __name__ == "__main__":
    main()
