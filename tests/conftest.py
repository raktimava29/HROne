import os
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from pymongo import MongoClient
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
    
# Load project .env before importing app.main.
load_dotenv()

from app import main  # noqa: E402


@pytest.fixture(scope="session")
def test_database():
    """Use an explicitly named test database; never fall back to MONGO_DB."""
    mongo_uri = os.getenv("MONGO_URI")
    test_db_name = os.getenv("MONGO_TEST_DB")
    normal_db_name = os.getenv("MONGO_DB", "attendance_db")

    if not mongo_uri:
        pytest.fail("MONGO_URI is missing; set it in your local .env.")
    if not test_db_name:
        pytest.fail(
            'MONGO_TEST_DB is missing. Set it for this PowerShell session, '
            'for example: $env:MONGO_TEST_DB = "attendance_test_db"'
        )
    if test_db_name == normal_db_name:
        pytest.fail(
            "MONGO_TEST_DB must differ from MONGO_DB. Refusing to run tests "
            "against the configured application database."
        )
    if "test" not in test_db_name.lower():
        pytest.fail(
            'MONGO_TEST_DB name must contain "test" to reduce risk of targeting '
            "a non-test database."
        )

    mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    database = mongo_client[test_db_name]

    try:
        database.command("ping")
    except Exception:
        mongo_client.close()
        pytest.fail("Cannot connect to MongoDB test database. Check MONGO_URI/network access.")

    yield database

    # Keep the test database; do not drop a database automatically.
    mongo_client.close()


@pytest.fixture
def api_client(test_database, monkeypatch):
    # Clear documents between tests, while retaining/recreating indexes.
    for collection_name in test_database.list_collection_names():
        test_database[collection_name].delete_many({})

    monkeypatch.setattr(main, "db", test_database)

    with TestClient(main.app) as client:
        yield client
