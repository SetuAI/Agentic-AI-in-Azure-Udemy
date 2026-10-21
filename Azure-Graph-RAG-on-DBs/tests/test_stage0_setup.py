import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def test_data_db_is_reachable():
    with psycopg.connect(os.environ["DATA_DB_URL"]) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_agent_db_has_vector_and_age():
    with psycopg.connect(os.environ["AGENT_DB_URL"]) as conn:
        names = {row[0] for row in conn.execute("SELECT extname FROM pg_extension")}
        preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0]
    assert {"vector", "age"} <= names
    assert "age" in preload
