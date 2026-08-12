import argparse
from dataclasses import dataclass
from pathlib import Path
import re

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Connection

from app.kb.vector_store import register_sqlite_vec
from app.llm.base import EMBED_DIM


@dataclass(frozen=True)
class IndexVerificationResult:
    chunks: int
    vectors: int
    dimension: int


def verify_index(conn: Connection) -> IndexVerificationResult:
    if conn.dialect.name != "sqlite":
        raise ValueError("knowledge index verification requires SQLite")

    chunks = int(conn.exec_driver_sql("SELECT COUNT(*) FROM kb_chunk").scalar_one())
    if chunks == 0:
        raise ValueError("knowledge chunk count must be nonzero")

    schema = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'vec_kb_chunk'"
    ).scalar_one_or_none()
    if not schema:
        raise ValueError("knowledge vector index is missing")

    dimension_match = re.search(r"\bembedding\s+float\[(\d+)\]", schema, re.IGNORECASE)
    if dimension_match is None:
        raise ValueError("knowledge vector index dimension is missing")
    dimension = int(dimension_match.group(1))
    if dimension != EMBED_DIM:
        raise ValueError(
            f"knowledge vector dimension mismatch: expected={EMBED_DIM} actual={dimension}"
        )

    vectors = int(conn.exec_driver_sql("SELECT COUNT(*) FROM vec_kb_chunk").scalar_one())
    if vectors != chunks:
        raise ValueError(f"knowledge vector count mismatch: chunks={chunks} vectors={vectors}")

    return IndexVerificationResult(chunks=chunks, vectors=vectors, dimension=dimension)


def verify_database_path(database_path: str | Path) -> IndexVerificationResult:
    path = Path(database_path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"knowledge database must be a file: {path}")

    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)))
    register_sqlite_vec(engine)
    try:
        with engine.connect() as connection:
            return verify_index(connection)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    result = verify_database_path(args.database)
    print(
        "index=ok "
        f"chunks={result.chunks} vectors={result.vectors} dimension={result.dimension}"
    )


if __name__ == "__main__":
    main()
