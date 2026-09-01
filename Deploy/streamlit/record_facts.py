"""Regenerates Frontend/reference/portfolio_facts.json from the portfolio database.

    python Deploy/streamlit/record_facts.py

The Methodology page states four counts, a date range and the five rows of
Risk_Limits. Where the database is present it reads them live; on the EC2 host it
reads them from that JSON file, because the host holds no copy of the portfolio
and giving it a database connection to print four counts would undo the reason it
holds none.

This script is how the file stops being a hand-maintained number. Run it after
any reload; it reads aggregates over a read-only connection and writes no row,
which is also why the output is safe in a public repository.
"""
from datetime import date
from pathlib import Path
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
DB = ROOT / "Database" / "credit_risk.db"
OUT = ROOT / "Frontend" / "reference" / "portfolio_facts.json"

COMMENT = [
    "Aggregate portfolio facts, recorded from Database/credit_risk.db so the",
    "Methodology page can state them where that file is absent -- which is the",
    "case on the EC2 host, whose whole point is that it holds no data and reaches",
    "no database. Counts, column counts, a date range and the five rows of",
    "Risk_Limits: every value here is an aggregate or a published threshold, and",
    "there is no loan or borrower row in it, which is why it is safe in a public",
    "repository.",
    "It lives under Frontend/reference/ and not Frontend/data/ because .gitignore",
    "excludes Data/ at any depth, case-insensitively -- so the obvious folder name",
    "would have kept this file out of the repository and off the EC2 clone, which",
    "is exactly the host that needs it.",
    "Where the database IS present the page reads it live and ignores this file.",
    "Regenerate with Deploy/streamlit/record_facts.py after any reload.",
]


def main() -> int:
    if not DB.exists():
        print("%s is not here. Build it with `python -m Database.seed_db`." % DB)
        return 1

    # Read-only URI: this script physically cannot write to the portfolio,
    # whatever the SQL below says.
    connection = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    cursor = connection.cursor()
    facts = {
        "_comment": COMMENT,
        "recorded_at": date.today().isoformat(),
        "recorded_from": "Database/credit_risk.db",
        "loans": cursor.execute("SELECT COUNT(*) FROM Loans").fetchone()[0],
        "borrowers": cursor.execute("SELECT COUNT(*) FROM Borrowers").fetchone()[0],
        "loan_columns": len(cursor.execute("PRAGMA table_info(Loans)").fetchall()),
        "borrower_columns": len(cursor.execute("PRAGMA table_info(Borrowers)").fetchall()),
    }
    facts["first_issue"], facts["last_issue"] = cursor.execute(
        "SELECT MIN(issue_date), MAX(issue_date) FROM Loans").fetchone()
    facts["limits"] = [
        list(row) for row in cursor.execute(
            "SELECT metric_name, threshold, source FROM Risk_Limits "
            "ORDER BY source, metric_name")
    ]
    connection.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(facts, handle, indent=2)
        handle.write("\n")

    print("%s\n  %d loans, %d borrowers, %d+%d columns, %s to %s, %d limits" % (
        OUT.relative_to(ROOT), facts["loans"], facts["borrowers"],
        facts["loan_columns"], facts["borrower_columns"],
        facts["first_issue"][:7], facts["last_issue"][:7], len(facts["limits"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
