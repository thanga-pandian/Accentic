"""Apply schema.sql idempotently. Safe to run repeatedly."""
import pathlib
from db.connection import get_connection

SCHEMA = pathlib.Path(__file__).parent / "schema.sql"


def main():
    sql = SCHEMA.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print("Migration complete.")


if __name__ == "__main__":
    main()
