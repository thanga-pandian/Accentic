import os
import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_connection() -> psycopg.Connection:
    url = os.environ["DATABASE_URL"]
    return psycopg.connect(url)
