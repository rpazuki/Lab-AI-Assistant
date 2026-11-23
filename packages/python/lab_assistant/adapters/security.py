"""Malware scanning and upload safety.

An unavailable scanner reports itself unavailable. It never reports ``clean``:
an out-of-date scanner that passes everything is worse than a recorded exception,
because the exception is visible in coverage metrics and the false pass is not.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ClamAvScanner", "ScanResult", "StubScanner"]


@dataclass(frozen=True, slots=True)
class ScanResult:
    clean: bool
    scanner: str
    signature_version: str | None = None
    detail: str | None = None
    #: False when the scan could not run, so ``clean`` False means "unscanned".
    available: bool = True


@dataclass
class ClamAvScanner:
    """ClamAV through ``clamdscan``, with signature freshness reported.

    The scanner lives in the parsing worker image. Signature age is surfaced so a
    stale database is an alert rather than an invisible weakening of the gate.
    """

    scanner_id: str = "clamav"
    binary: str = "clamdscan"
    database_path: Path = Path("/var/lib/clamav")
    timeout_seconds: float = 60.0

    async def scan(self, data: bytes, *, filename: str | None = None) -> ScanResult:
        if shutil.which(self.binary) is None:
            return ScanResult(
                clean=False,
                scanner=self.scanner_id,
                detail=f"{self.binary} is not installed in this image",
                available=False,
            )
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "-",
            "--no-summary",
            "--stream",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(data), timeout=self.timeout_seconds
            )
        except TimeoutError:
            process.kill()
            return ScanResult(
                clean=False, scanner=self.scanner_id, detail="scan timed out", available=False
            )
        output = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
        if process.returncode == 0:
            return ScanResult(
                clean=True,
                scanner=self.scanner_id,
                signature_version=await self._signature_version(),
                detail=output.strip() or None,
            )
        if process.returncode == 1:
            return ScanResult(
                clean=False, scanner=self.scanner_id, detail=f"malware detected: {output.strip()}"
            )
        # Exit 2 and above is clamdscan failing (no daemon, no socket), not a find.
        return ScanResult(
            clean=False,
            scanner=self.scanner_id,
            detail=f"scanner error (exit {process.returncode}): {output.strip()}",
            available=False,
        )

    async def signature_age_days(self) -> float | None:
        candidates = list(self.database_path.glob("*.c?d")) if self.database_path.is_dir() else []
        if not candidates:
            return None
        from datetime import UTC, datetime

        newest = max(path.stat().st_mtime for path in candidates)
        return (datetime.now(tz=UTC).timestamp() - newest) / 86400

    async def _signature_version(self) -> str | None:
        age = await self.signature_age_days()
        return f"age_days={age:.1f}" if age is not None else None


@dataclass
class StubScanner:
    """Test scanner: matches configured byte signatures.

    Used by the malicious-fixture tests. It is not permissive by default: a
    payload it does not recognise is reported clean only because the test fixture
    set defines what "malicious" means for that test.
    """

    scanner_id: str = "stub"
    malicious_markers: tuple[bytes, ...] = field(
        default_factory=lambda: (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR", b"<script>evil")
    )
    available: bool = True

    async def scan(self, data: bytes, *, filename: str | None = None) -> ScanResult:
        if not self.available:
            return ScanResult(
                clean=False, scanner=self.scanner_id, detail="scanner unavailable", available=False
            )
        for marker in self.malicious_markers:
            if marker in data:
                return ScanResult(
                    clean=False,
                    scanner=self.scanner_id,
                    signature_version="stub-1",
                    detail=f"matched marker {marker[:16]!r}",
                )
        return ScanResult(clean=True, scanner=self.scanner_id, signature_version="stub-1")

    async def signature_age_days(self) -> float | None:
        return 0.0
