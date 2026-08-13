"""Explicit row mapping between domain models and Core tables.

Deliberately not the ORM. Domain objects are pydantic models with no database
awareness, and rows are dictionaries: no identity map, no lazy relationship, and
therefore no chance of a relationship loading inside an async response and
raising ``MissingGreenlet`` — a failure mode the predecessor documented.

The mapper handles the three mechanical differences between a model and a row:
column renames, sequences stored as text arrays, and enum values.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Table

__all__ = ["Mapper", "uuid_list", "uuid_or_none"]

TModel = TypeVar("TModel", bound=BaseModel)


def uuid_or_none(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def uuid_list(value: Any) -> tuple[UUID, ...]:
    if not value:
        return ()
    return tuple(UUID(str(item)) for item in value)


def _to_column(value: Any) -> Any:
    """Coerce a model value into something a Core column accepts."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (frozenset, set)):
        return sorted(_stringify(item) for item in value)
    if isinstance(value, tuple):
        # Sequences land in text-array columns, so identifiers become their
        # canonical string form rather than a driver-specific UUID object.
        return [_stringify(item) if isinstance(item, UUID) else _to_column(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _to_column(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify(item) if isinstance(item, UUID) else _to_column(item) for item in value]
    return value


def _stringify(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _from_column(value: Any) -> Any:
    """Normalise a driver value on read.

    Databases without a timezone-aware type (SQLite) return naive datetimes.
    Storage is UTC by contract, so the timezone is reattached here rather than
    letting a naive value reach the domain and break a comparison later.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass(frozen=True)
class Mapper:
    """Maps one model type onto one table.

    ``renames`` maps model field -> column name where they differ. ``loaders``
    supplies per-field conversions on read (for example text arrays back into
    tuples of UUID).
    """

    table: Table
    model: type[BaseModel]
    renames: Mapping[str, str] = field(default_factory=dict)
    loaders: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)
    #: Columns the table has but the model does not (computed on write).
    computed: Mapping[str, Callable[[BaseModel], Any]] = field(default_factory=dict)

    def column_for(self, field_name: str) -> str:
        return self.renames.get(field_name, field_name)

    def to_row(self, instance: BaseModel) -> dict[str, Any]:
        row: dict[str, Any] = {}
        columns = set(self.table.columns.keys())
        for field_name in type(instance).model_fields:
            column = self.column_for(field_name)
            if column not in columns:
                continue
            row[column] = _to_column(getattr(instance, field_name))
        for column, compute in self.computed.items():
            row[column] = _to_column(compute(instance))
        return row

    def from_row(self, row: Mapping[str, Any]) -> Any:
        values: dict[str, Any] = {}
        for field_name in self.model.model_fields:
            column = self.column_for(field_name)
            if column not in row:
                continue
            value = row[column]
            loader = self.loaders.get(field_name)
            values[field_name] = loader(value) if loader is not None else _from_column(value)
        return self.model.model_validate(values)

    def from_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[Any]:
        return [self.from_row(row) for row in rows]

    def upsert_values(self, instance: BaseModel) -> tuple[dict[str, Any], Sequence[str]]:
        """Row values plus the primary-key column names, for an upsert."""
        row = self.to_row(instance)
        keys = [column.name for column in self.table.primary_key.columns]
        return row, keys
