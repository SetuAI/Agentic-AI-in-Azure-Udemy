# =============================================================================
# tests/test_stage0_setup.py
# =============================================================================
# WHAT THIS FILE IS FOR
#   The very first check (Stage 0): can we reach both Azure PostgreSQL databases,
#   and does agent_meta have the pgvector and AGE extensions switched on?
#   If these fail, nothing else in the project can work - fix .env first.
#
# HOW TO RUN (from the project folder)
#   Windows (PowerShell):  uv run pytest tests/test_stage0_setup.py -v
#   Mac / Linux:           uv run pytest tests/test_stage0_setup.py -v
#
# HOW PYTEST WORKS (for freshers)
#   pytest finds every function whose name starts with "test_" and runs it.
#   Inside, "assert <condition>" must be True; if it is False the test FAILS.
# =============================================================================
# os: reads DATA_DB_URL and AGENT_DB_URL.
import os

# psycopg: the PostgreSQL driver.
import psycopg

# load_dotenv: copies .env values into environment variables.
from dotenv import load_dotenv

# Read .env as soon as the test file is loaded.
load_dotenv()


# TEST 1: mf_data answers a trivial query.
def test_data_db_is_reachable():
    # Connect to mf_data (closed automatically at the end of the block).
    with psycopg.connect(os.environ["DATA_DB_URL"]) as conn:
        # "SELECT 1" must return the number 1.
        assert conn.execute("SELECT 1").fetchone()[0] == 1


# TEST 2: agent_meta has both extensions, and AGE is preloaded.
def test_agent_db_has_vector_and_age():
    # Connect to agent_meta.
    with psycopg.connect(os.environ["AGENT_DB_URL"]) as conn:
        # The names of all installed extensions, as a set.
        names = {row[0] for row in conn.execute("SELECT extname FROM pg_extension")}
        # The libraries the server loads at start-up.
        preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0]
    # "<=" on sets means "is contained in": both must be installed.
    assert {"vector", "age"} <= names
    # AGE must be preloaded, or cypher() calls fail.
    assert "age" in preload
