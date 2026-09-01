#!/usr/bin/env python3
"""Phase 6, step 2 of 3: SQLite -> CSV, for PostgreSQL's COPY to read.

Runs on the EC2 app host with nothing but the standard library -- no pandas, no
psycopg2, no pip install. That is deliberate: this is the one script that has to
work on a bare Amazon Linux box before anything else is set up, and every
dependency it does not have is a dependency that cannot be missing.

Why CSV and not `sqlite3 .dump | psql`
-------------------------------------
The dump route is what the first draft of the deployment guide suggested, and it
does not survive contact with this schema. A SQLite dump emits its own CREATE
TABLE statements (with AUTOINCREMENT, which PostgreSQL rejects), wraps everything
in one transaction of 878,317 single-row INSERTs, and hands PostgreSQL SQLite's
loose typing verbatim. Hand-editing that file is exactly the kind of step that
appears to work and silently corrupts a column.

COPY is also an order of magnitude faster than row-at-a-time INSERT, which
matters on a db.t4g.micro with burstable CPU: the load is over before the credit
balance notices.

The NULL convention
-------------------
PostgreSQL's `COPY ... WITH (FORMAT csv, NULL '')` reads an *unquoted* empty
field as NULL and a *quoted* empty field ("") as the empty string. Python's
`csv` module cannot express that distinction on 3.9 (QUOTE_NOTNULL arrived in
3.12), and QUOTE_MINIMAL renders None and '' identically -- which would turn
77,824 NULL job titles into empty strings, or the reverse. So the rows are
formatted here directly: None becomes a bare empty field, and every other value
is quoted. 03_verify.sql asserts both counts afterwards.

Column order
------------
The generated `load_<table>.sql` names its columns explicitly, in the order
PRAGMA table_info reports them, which is the order the SELECT returns. A
mis-ordered load -- the failure mode that puts int_rate in the installment
column and raises nothing -- is therefore not expressible.

Usage:
    python3 export_csv.py credit_risk.db ./out
"""

import os
import sqlite3
import sys
import time

# Source table -> destination table. PostgreSQL folds unquoted identifiers to
# lowercase and every SQL string in the repo is unquoted, so these are the names
# 01_schema.sql creates.
TABLES = {
    "Loans": "loans",
    "Borrowers": "borrowers",
    "Risk_Limits": "risk_limits",
}

FETCH_ROWS = 20_000


def format_field(value):
    """One CSV field, following the NULL convention described in the docstring."""
    if value is None:
        return ""
    if isinstance(value, float):
        # repr() is the shortest string that round-trips back to the same double,
        # so the sums in 03_verify.sql match the source exactly rather than
        # nearly. A fixed-precision format would lose the last digit.
        return '"' + repr(value) + '"'
    if isinstance(value, int):
        return '"' + str(value) + '"'
    return '"' + str(value).replace('"', '""') + '"'


def export_table(con, source, target, out_dir):
    columns = [row[1] for row in con.execute(f'PRAGMA table_info("{source}")')]
    if not columns:
        raise SystemExit(f"table {source} not found in the SQLite file")

    csv_path = os.path.join(out_dir, f"{target}.csv")
    sql_path = os.path.join(out_dir, f"load_{target}.sql")

    cur = con.cursor()
    cur.arraysize = FETCH_ROWS
    quoted = ", ".join(f'"{c}"' for c in columns)
    cur.execute(f'SELECT {quoted} FROM "{source}"')

    started = time.time()
    rows = 0
    with open(csv_path, "w", encoding="utf-8", newline="\n") as handle:
        while True:
            batch = cur.fetchmany(FETCH_ROWS)
            if not batch:
                break
            handle.write(
                "".join(
                    ",".join(format_field(v) for v in row) + "\n" for row in batch
                )
            )
            rows += len(batch)
            if rows % 200_000 == 0:
                print(f"  {target}: {rows:,} rows", flush=True)

    with open(sql_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            f"\\copy {target} ({', '.join(columns)}) "
            f"FROM '{target}.csv' WITH (FORMAT csv, NULL '')\n"
        )

    size_mb = os.path.getsize(csv_path) / (1024 * 1024)
    print(
        f"{target}: {rows:,} rows, {len(columns)} columns, "
        f"{size_mb:.1f} MiB, {time.time() - started:.1f}s",
        flush=True,
    )
    return rows


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__.strip().splitlines()[-1].strip())

    db_path, out_dir = sys.argv[1], sys.argv[2]
    if not os.path.exists(db_path):
        raise SystemExit(f"no such SQLite file: {db_path}")
    os.makedirs(out_dir, exist_ok=True)

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = sum(
            export_table(con, source, target, out_dir)
            for source, target in TABLES.items()
        )
    finally:
        con.close()

    print(f"\n{total:,} rows exported to {out_dir}")


if __name__ == "__main__":
    main()
