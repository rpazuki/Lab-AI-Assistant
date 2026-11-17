"""Reproducible corpora for tests, drills and the archive format work.

Not test helpers in the throwaway sense: these build their material through the
same services a deployment runs, so a fixture that still works is evidence the
lifecycle still works.
"""

from lab_assistant.fixtures.archive import (
    ARCHIVE_FIXTURE_CASES,
    ArchiveFixture,
    publish_archive_fixture,
)

__all__ = ["ARCHIVE_FIXTURE_CASES", "ArchiveFixture", "publish_archive_fixture"]
