import uuid

import httpx
import psycopg
from app.main import Health, Item, database_url
from psycopg.rows import class_row


def test_health_checks_postgres() -> None:
    response = httpx.get("http://127.0.0.1:8000/health", timeout=10)
    response.raise_for_status()
    assert Health.model_validate_json(response.content).status == "ok"
    with psycopg.connect(database_url()) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_created_item_is_persisted_in_postgres() -> None:
    name = f"example-{uuid.uuid4().hex}"
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
        response = client.post("/items", json={"name": name})
        assert response.status_code == 201
        item = Item.model_validate_json(response.content)
        try:
            assert item.name == name
            with psycopg.connect(database_url(), row_factory=class_row(Item)) as connection:
                persisted = connection.execute("SELECT id, name FROM items WHERE id = %s", (item.id,)).fetchone()
            assert persisted == item
        finally:
            assert client.delete(f"/items/{item.id}").status_code == 204
        with psycopg.connect(database_url()) as connection:
            assert connection.execute("SELECT id FROM items WHERE id = %s", (item.id,)).fetchone() is None
