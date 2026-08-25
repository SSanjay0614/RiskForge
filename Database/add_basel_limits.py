"""
One-time addition of Basel III-sourced regulatory floors to Risk_Limits.
Run once: python -m Database.add_basel_limits
"""

import sqlite3

from config import DB_PATH


BASEL_LIMITS = [
    (
        "pd_floor_retail_other", 0.0005, "basel_iii",
        "CRE32.58 -- PD floor for retail exposures other than QRRE revolvers (0.05%)",
    ),
    (
        "lgd_floor_retail_unsecured_other", 0.30, "basel_iii",
        "CRE32.58 -- LGD floor for unsecured 'other retail' exposures (30%)",
    ),
]


def add_basel_limits():
    conn = sqlite3.connect(DB_PATH)

    existing = {
        row[0] for row in conn.execute("SELECT metric_name FROM Risk_Limits").fetchall()
    }

    to_insert = [row for row in BASEL_LIMITS if row[0] not in existing]

    if not to_insert:
        print("Basel limits already present, nothing to add.")
        conn.close()
        return

    conn.executemany(
        "INSERT INTO Risk_Limits (metric_name, threshold, source, description) VALUES (?, ?, ?, ?)",
        to_insert,
    )
    conn.commit()

    print(f"Added {len(to_insert)} Basel-sourced risk limit(s).")

    for row in conn.execute("SELECT metric_name, threshold, source, description FROM Risk_Limits"):
        print(f"  {row}")

    conn.close()


if __name__ == "__main__":
    add_basel_limits()
