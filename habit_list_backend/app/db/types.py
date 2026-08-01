"""Cross-dialect database types used by the production schema."""
from __future__ import annotations

from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import JSON, Float
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

MEMORY_EMBEDDING_DIMENSION = 1024


class VectorStorage(TypeDecorator[list[float]]):
    """Store vectors as pgvector in PostgreSQL and JSON in local SQLite.

    Keeping one mapped attribute lets the Memory V2 service run unchanged in
    local tests while production gets a real vector column and ANN indexes.
    """

    impl = JSON
    cache_ok = True

    class comparator_factory(TypeDecorator.Comparator):
        def cosine_distance(self, other):
            """Use pgvector's cosine operator without casting away the ANN index."""
            return self.op("<=>", return_type=Float)(other)

    def __init__(self, dimension: int = MEMORY_EMBEDDING_DIMENSION):
        super().__init__()
        self.dimension = dimension

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(VECTOR(self.dimension))
        return dialect.type_descriptor(JSON())

    def __repr__(self) -> str:
        return f"VectorStorage(dimension={self.dimension})"


__all__ = ["MEMORY_EMBEDDING_DIMENSION", "VectorStorage"]
