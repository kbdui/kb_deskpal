"""Read assertions over synthetic SQLite fixtures."""
from contextlib import closing
import sqlite3

def sql_rows(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql, parameters).fetchall()
