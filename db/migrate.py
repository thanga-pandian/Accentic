"""Apply schema idempotently. Detects Postgres vs SQLite from DATABASE_URL."""
import pathlib
from db.connection import get_connection, DATABASE_URL

HERE = pathlib.Path(__file__).parent


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
