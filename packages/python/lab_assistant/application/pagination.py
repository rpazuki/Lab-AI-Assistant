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
from typing import Any, Protocol
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


async def every_session(uow: Any, workspace_id: UUID) -> list[Any]:
    """Every member's conversations, for what must see all of them (retraction, retention)."""
    return await collect_pages(
        lambda after, size: uow.chat.list_workspace_sessions(
            workspace_id, limit=size, after_id=after
        )
    )


async def every_release(uow: Any, workspace_id: UUID) -> list[Any]:
    """Every release, newest first, for what must see all of them."""
    releases = await collect_pages(
        lambda after, size: uow.releases.list_all_releases(workspace_id, limit=size, after_id=after)
    )
    return sorted(
        releases,
        key=lambda release: (release.created_at is not None, release.created_at),
        reverse=True,
    )


def slice_page[T](
    rows: Sequence[T],
    *,
    key: Callable[[T], str],
    limit: int,
    after: str | None,
    descending: bool = False,
) -> tuple[list[T], str | None]:
    """One page of a list built in memory, with the cursor to the next.

    ``rows`` must already be ordered by ``key`` (descending when ``descending``),
    and ``key`` must be unique, so the cursor is the last row's key and a page
    resumes after it even when rows were added or removed in between.
    """
    if after is not None:
        rows = [row for row in rows if (key(row) < after if descending else key(row) > after)]
    page = list(rows[:limit])
    return page, key(page[-1]) if len(rows) > limit and page else None
