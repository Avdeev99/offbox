import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Response
from psycopg.rows import class_row
from pydantic import BaseModel, Field


class ItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class Item(ItemInput):
    id: int


class Health(BaseModel):
    status: str


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://offbox:offbox@127.0.0.1:5432/offbox")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
    with psycopg.connect(database_url()) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS items (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL)")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health() -> Health:
    with psycopg.connect(database_url()) as connection:
        row = connection.execute("SELECT 1").fetchone()
    if row != (1,):
        raise HTTPException(status_code=503, detail="Database is not ready")
    return Health(status="ok")


@app.post("/items", status_code=201)
def create_item(payload: ItemInput) -> Item:
    with psycopg.connect(database_url(), row_factory=class_row(Item)) as connection:
        item = connection.execute("INSERT INTO items (name) VALUES (%s) RETURNING id, name", (payload.name,)).fetchone()
    if item is None:
        raise HTTPException(status_code=500, detail="Item was not created")
    return item


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int) -> Response:
    with psycopg.connect(database_url()) as connection:
        deleted = connection.execute("DELETE FROM items WHERE id = %s RETURNING id", (item_id,)).fetchone()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return Response(status_code=204)
