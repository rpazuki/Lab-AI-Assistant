"""The acquisition queue as a spreadsheet, for fetching papers by hand.

What no route could fetch is downloaded by a person, from a library, a
publisher's site or the author. This lists the plans a search of the queue
finds, every page of them, with the links a person opens to look for each paper:
one CSV, or a zip of CSVs of at most :data:`ROWS_PER_FILE` rows, so the work
can be shared out or done a batch at a time.

The ``plan_id`` column ties each row back to its plan, so a copy found this way
is uploaded against the right paper. Cells are formula-escaped like any export:
a title comes from untrusted metadata.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from lab_assistant.application.context import ActorContext, ServiceContext
from lab_assistant.application.export import escape_spreadsheet_cell
from lab_assistant.contracts.connector import DOI_SCHEMES
from lab_assistant.domain.assets import AcquisitionAttempt, AcquisitionPlan

__all__ = ["COLUMNS", "ROWS_PER_FILE", "AcquisitionExportService", "rows_csv", "rows_zip"]

#: The most rows in one file of the zip.
ROWS_PER_FILE = 100

#: Plans read from the database at a time.
_BATCH = 500

COLUMNS = (
    "title",
    "first_author",
    "year",
    "journal",
    "doi",
    "pmid",
    "pmcid",
    "arxiv",
    #: The link to start from: the DOI's, else PubMed Central's, PubMed's or arXiv's.
    "url",
    "doi_url",
    "pmc_url",
    "pubmed_url",
    "status",
    "last_outcome",
    #: Where the last fetch looked, query string removed: often the PDF itself.
    "last_tried_url",
    "licence",
    "plan_id",
    "work_id",
)


def _links(identifiers: dict[str, str]) -> dict[str, str]:
    doi, pmcid, pmid, arxiv = (identifiers.get(k, "") for k in ("doi", "pmcid", "pmid", "arxiv"))
    links = {
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "pmc_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/" if pmcid else "",
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
    }
    arxiv_url = f"https://arxiv.org/abs/{arxiv}" if arxiv else ""
    links["url"] = links["doi_url"] or links["pmc_url"] or links["pubmed_url"] or arxiv_url
    return links


def _latest(attempts: Sequence[AcquisitionAttempt]) -> AcquisitionAttempt | None:
    """The latest attempt, ordered as the queue's "last answer" filter orders them."""
    if not attempts:
        return None
    return max(
        attempts,
        key=lambda a: (a.requested_at is not None, a.requested_at, a.attempt_number),
    )


def rows_csv(rows: Iterable[dict[str, Any]]) -> bytes:
    """One CSV with a header, formula-escaped, with a byte-order mark for Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([escape_spreadsheet_cell(row.get(column)) for column in COLUMNS])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def rows_zip(rows: Sequence[dict[str, Any]], stem: str, per_file: int = ROWS_PER_FILE) -> bytes:
    """CSVs of at most ``per_file`` rows each, every one with its header, zipped.

    The files are numbered in the queue's order with enough digits to sort as
    names: ``stem-001.csv``, ``stem-002.csv``. No rows is one file with a header.
    """
    batches = [rows[start : start + per_file] for start in range(0, len(rows), per_file)] or [[]]
    width = max(3, len(str(len(batches))))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for number, batch in enumerate(batches, start=1):
            archive.writestr(f"{stem}-{number:0{width}d}.csv", rows_csv(batch))
    return buffer.getvalue()


class AcquisitionExportService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    async def rows(
        self,
        actor: ActorContext,
        *,
        status: str | None = None,
        words: tuple[str, ...] = (),
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every plan the queue's search finds, in the queue's order, one row each.

        The same filters as the list, and the same people: whoever sees the
        queue can take it away to work through.
        """
        ws = actor.workspace_id
        rows: list[dict[str, Any]] = []
        after: UUID | None = None
        async with self._context.uow_factory() as uow:
            while True:
                plans: list[AcquisitionPlan] = await uow.assets.list_plans(
                    ws, status=status, limit=_BATCH, after_id=after, words=words, outcome=outcome
                )
                if not plans:
                    break
                work_ids = list(dict.fromkeys(plan.work_id for plan in plans))
                summaries = await uow.works.get_summaries(ws, work_ids)
                identifiers = await uow.works.list_identifiers_for_works(ws, work_ids)
                attempts = await uow.assets.list_attempts_for_plans(ws, [plan.id for plan in plans])
                for plan in plans:
                    rows.append(
                        self._row(
                            plan,
                            summaries.get(plan.work_id),
                            identifiers.get(plan.work_id, []),
                            _latest(attempts.get(plan.id, [])),
                        )
                    )
                if len(plans) < _BATCH:
                    break
                after = plans[-1].id
        return rows

    @staticmethod
    def _row(
        plan: AcquisitionPlan,
        summary: Any,
        identifiers: Sequence[Any],
        latest: AcquisitionAttempt | None,
    ) -> dict[str, Any]:
        # One value per scheme, the first in a stable order; any DOI scheme is the DOI.
        values: dict[str, str] = {}
        for identifier in identifiers:
            scheme = identifier.scheme.value
            key = "doi" if scheme in DOI_SCHEMES else scheme
            values.setdefault(key, identifier.value_display)
        return {
            "title": getattr(summary, "title", None),
            "first_author": getattr(summary, "first_author", None),
            "year": getattr(summary, "year", None),
            "journal": getattr(summary, "journal", None),
            "doi": values.get("doi"),
            "pmid": values.get("pmid"),
            "pmcid": values.get("pmcid"),
            "arxiv": values.get("arxiv"),
            **_links(values),
            "status": plan.status,
            "last_outcome": (
                "never tried"
                if latest is None
                else latest.outcome.value
                if latest.outcome
                else "pending"
            ),
            "last_tried_url": latest.source_url_redacted if latest else None,
            "licence": getattr(summary, "licence", None),
            "plan_id": str(plan.id),
            "work_id": str(plan.work_id),
        }
