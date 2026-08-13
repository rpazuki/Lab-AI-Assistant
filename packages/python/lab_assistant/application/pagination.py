"""Keyset pagination for lifecycle paths that must see everything.

A ``limit`` is a page size. It is never a statement about how much of a corpus a
stage may consider — and the difference matters, because the predecessor corpus is
already larger than several of the fixed limits this module replaces. A stage that
silently stops at 1,000 works produces a release that looks complete, passes its
gates, and omits material nobody can name afterwards.

Usage::

    works = await collect_pages(
        lambda after, size: uow.works.list_works(
            workspace_id, relevance="included", limit=size, after_id=after
        )
    )

The cursor is the row id: total, stable, and unaffected by concurrent inserts
landing between pages, which a numeric offset is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Protocol
from uuid import UUID

__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGES", "collect_pages", "iter_pages"]

#: Big enough that a normal corpus is a handful of round trips, small enough that
#: one page never becomes the memory spike it was meant to prevent.
DEFAULT_PAGE_SIZE = 500

#: Runaway guard. At the default page size this is five million rows — far beyond
#: any real corpus, and low enough that a repository bug returning the same page
#: forever fails loudly instead of looping until the process dies.
MAX_PAGES = 10_000


class _HasId(Protocol):
    id: UUID


async def iter_pages[T: _HasId](
    fetch: Callable[[UUID | None, int], Awaitable[Sequence[T]]],
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AsyncIterator[T]:
    """Yield every row ``fetch`` can produce, one page at a time."""
    cursor: UUID | None = None
    for _ in range(MAX_PAGES):
        page = await fetch(cursor, page_size)
        if not page:
            return
        for row in page:
            yield row
        if len(page) < page_size:
            return
        next_cursor = page[-1].id
        if next_cursor == cursor:  # pragma: no cover - repository contract violation
            raise RuntimeError(
                "pagination did not advance: the repository returned the same cursor twice"
            )
        cursor = next_cursor
    raise RuntimeError(  # pragma: no cover - runaway guard
        f"pagination exceeded {MAX_PAGES} pages; the repository is not advancing"
    )


async def collect_pages[T: _HasId](
    fetch: Callable[[UUID | None, int], Awaitable[Sequence[T]]],
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[T]:
    """Every row, materialised. Use when the caller genuinely needs them all."""
    return [row async for row in iter_pages(fetch, page_size=page_size)]
