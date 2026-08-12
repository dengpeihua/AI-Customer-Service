import sqlite_vec
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from app.llm.base import EMBED_DIM, Vector


def register_sqlite_vec(engine: Engine) -> None:
    """Load the sqlite-vec extension on every new DBAPI connection."""

    @event.listens_for(engine, "connect")
    def _load(dbapi_conn, _record):
        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)


class VectorStore:
    """sqlite-vec backed vector index. All rows carry tenant_id for isolation."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def ensure_schema(self) -> None:
        self._conn.exec_driver_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_kb_chunk USING vec0("
            f"chunk_id integer primary key, tenant_id integer, "
            f"embedding float[{EMBED_DIM}] distance_metric=cosine)"
        )

    def recreate_schema(self) -> None:
        """Replace only the derived vector index, preserving source chunks."""
        self._conn.exec_driver_sql("DROP TABLE IF EXISTS vec_kb_chunk")
        self.ensure_schema()

    def add(self, tenant_id: int, chunk_id: int, embedding: Vector) -> None:
        self._conn.exec_driver_sql(
            "INSERT INTO vec_kb_chunk(chunk_id, tenant_id, embedding) VALUES (?, ?, ?)",
            (chunk_id, tenant_id, sqlite_vec.serialize_float32(embedding)),
        )

    def delete(self, tenant_id: int, chunk_ids: list[int]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" * len(chunk_ids))
        self._conn.exec_driver_sql(
            f"DELETE FROM vec_kb_chunk WHERE tenant_id = ? AND chunk_id IN ({placeholders})",
            (tenant_id, *chunk_ids),
        )

    def clear(self) -> None:
        self._conn.exec_driver_sql("DELETE FROM vec_kb_chunk")

    def count(self) -> int:
        row = self._conn.exec_driver_sql(
            "SELECT COUNT(*) FROM vec_kb_chunk"
        ).one()
        return int(row[0])

    def search(self, tenant_id: int, query: Vector, k: int) -> list[tuple[int, float]]:
        rows = self._conn.exec_driver_sql(
            "SELECT chunk_id, distance FROM vec_kb_chunk "
            "WHERE embedding MATCH ? AND tenant_id = ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(query), tenant_id, k),
        ).fetchall()
        return [(int(cid), float(dist)) for cid, dist in rows]
