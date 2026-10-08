"""Apply schema idempotently. Detects Postgres vs SQLite from DATABASE_URL."""
import pathlib
import re

from db.connection import get_connection, DATABASE_URL

HERE = pathlib.Path(__file__).parent


def _add_missing_sqlite_columns(cur, sql: str):
    """CREATE TABLE IF NOT EXISTS never alters an existing table, so add new columns."""
    for table, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S
    ):
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        for line in body.splitlines():
            m = re.match(r"\s+(\w+)\s+(TEXT|INTEGER|REAL)(.*?),?$", line)
            if not m or m.group(1) in existing:
                continue
            col, typ, rest = m.groups()
            default = re.search(r"DEFAULT\s+('[^']*'|[\d.]+)", rest)
            ddl = f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
            if default:
                ddl += f" NOT NULL DEFAULT {default.group(1)}" if "NOT NULL" in rest \
                    else f" DEFAULT {default.group(1)}"
            cur.execute(ddl)


def main():
    if DATABASE_URL.startswith("sqlite:///"):
        schema_file = HERE / "sqlite_schema.sql"
    else:
        schema_file = HERE / "schema.sql"

    sql = schema_file.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        # SQLite needs statements run one at a time; split on semicolons
        if DATABASE_URL.startswith("sqlite:///"):
            with conn.cursor() as cur:
                for stmt in sql.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        cur.execute(stmt)
                _add_missing_sqlite_columns(cur, sql)
        else:
            with conn.cursor() as cur:
                cur.execute(sql)
        conn.commit()
        db_label = DATABASE_URL.split("///")[-1] if "sqlite" in DATABASE_URL else DATABASE_URL.split("@")[-1]
        print(f"Migration complete ({db_label}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
