#!/usr/bin/env python3
"""Build CiteIntegrity's compact DOI event index from Crossref's RW CSV."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


def doi(value: str) -> str:
    match = DOI_RE.search(str(value or ""))
    return match.group(0).rstrip(".,;:)]}").lower() if match else ""


def event_type(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", " ")
    if "reinstate" in raw:
        return "reinstatement"
    if "expression" in raw and "concern" in raw:
        return "expression_of_concern"
    if "retract" in raw:
        return "retraction"
    if "withdraw" in raw:
        return "withdrawal"
    if any(token in raw for token in ("correction", "corrigendum", "erratum")):
        return "correction"
    return "other_update"


def event_date(value: str) -> str:
    raw = str(value or "").strip()
    for pattern in ("%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--dataset-date", default="")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    connection = sqlite3.connect(output)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE events (
            original_doi TEXT NOT NULL,
            event_type TEXT NOT NULL,
            label TEXT NOT NULL,
            notice_doi TEXT NOT NULL,
            event_date TEXT NOT NULL,
            source TEXT NOT NULL,
            record_id TEXT NOT NULL,
            PRIMARY KEY (original_doi, event_type, notice_doi, event_date, record_id)
        );
        CREATE INDEX events_original_doi_idx ON events(original_doi);
        CREATE TABLE notices (
            notice_doi TEXT PRIMARY KEY
        );
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    event_rows = []
    notice_rows = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            original = doi(row.get("OriginalPaperDOI", ""))
            notice = doi(row.get("RetractionDOI", ""))
            if notice:
                notice_rows.add(notice)
            if not original:
                continue
            nature = str(row.get("RetractionNature") or "Publication update").strip()
            event_rows.append((
                original,
                event_type(nature),
                nature,
                notice,
                event_date(row.get("RetractionDate") or ""),
                "retraction-watch",
                str(row.get("Record ID") or "").strip(),
            ))
            if len(event_rows) >= 5000:
                connection.executemany(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    event_rows,
                )
                event_rows.clear()

    if event_rows:
        connection.executemany(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            event_rows,
        )
    connection.executemany(
        "INSERT OR IGNORE INTO notices(notice_doi) VALUES (?)",
        [(value,) for value in sorted(notice_rows)],
    )
    metadata = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "dataset_date": args.dataset_date,
        "source_commit": args.source_commit,
        "source_url": "https://gitlab.com/crossref/retraction-watch-data",
    }
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
    )
    connection.commit()
    connection.execute("VACUUM")
    count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    notices = connection.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    connection.close()
    print(f"Built {output}: {count} event rows, {notices} notice DOIs")


if __name__ == "__main__":
    main()
