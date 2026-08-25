import sqlite3

from config import DB_PATH, DATABASE_DIR


def init_db():

    DATABASE_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    schema_path = DATABASE_DIR / "schema.sql"
    with open(schema_path) as f:
        conn.executescript(f.read())

    conn.commit()

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    print(f"Database created at: {DB_PATH}")
    print(f"Tables: {[t[0] for t in tables]}")

    conn.close()


if __name__ == "__main__":
    init_db()