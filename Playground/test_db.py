import sqlite3
conn = sqlite3.connect("Database/credit_risk.db")
print(conn.execute("SELECT COUNT(*) FROM Loans").fetchone())
conn.close()