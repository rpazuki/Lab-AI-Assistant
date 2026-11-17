"""Logical dump and restore of the control plane, through `pg_dump`.

An adapter rather than a service because it runs external programs. That has two
consequences worth stating, since both were wrong in the scripts this replaces.

**The password never reaches a command line.** `pg_dump` takes it from
`PGPASSWORD` in the child's environment, so it is not in `ps` output and not in
the operator's shell history. The connection parameters are passed as flags; the
secret is not a parameter.

**The tool's own version is recorded.** A dump written by `pg_dump` 17 does not
replay into `pg_restore` 16, and the failure arrives partway through a restore
rather than at the start of one. Recording the banner lets the preflight say so
before anybody has dropped anything.

The dump is the custom format (`--format=custom`): compressed, and restorable
selectively, which matters when a restore has to skip an object that will not
replay rather than abandon the whole run.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from lab_assistant.application.ports import DatabaseFingerprint, DumpResult
from lab_assistant.contracts.errors import ErrorClass, LabAssistantError

__all__ = ["PostgresDatabaseSnapshot", "connection_parameters"]


def connection_parameters(database_url: str) -> dict[str, str]:
    """Split a SQLAlchemy URL into libpq connection parameters.

    Returns host, port, user, dbname and password as separate entries so the
    caller can put the password in the environment and everything else on the
    command line. Percent-encoded credentials are decoded here: libpq wants the
    literal value, and a password containing `@` would otherwise arrive
    mangled — in a way that looks like an authentication failure.
    """
    parts = urlsplit(database_url.replace("+asyncpg", "").replace("+psycopg", ""))
    values = {
        "host": parts.hostname or "localhost",
        "port": str(parts.port or 5432),
        "user": unquote(parts.username or ""),
        "dbname": parts.path.lstrip("/"),
    }
    if parts.password:
        values["password"] = unquote(parts.password)
    return values


class PostgresDatabaseSnapshot:
    """`DatabaseSnapshot` over a real PostgreSQL, via the engine and the CLI tools."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        database_url: str,
        pg_dump: str = "pg_dump",
        pg_restore: str = "pg_restore",
        timeout_seconds: float = 3600.0,
    ) -> None:
        self._engine = engine
        self._url = database_url
        self._pg_dump = pg_dump
        self._pg_restore = pg_restore
        self._timeout = timeout_seconds

    # ── reading what the server is ─────────────────────────────────────────

    async def fingerprint(self) -> DatabaseFingerprint:
        if self._engine.dialect.name != "postgresql":
            raise LabAssistantError(
                "a portable backup needs PostgreSQL",
                context={
                    "dialect": self._engine.dialect.name,
                    "why": "the dump format, the extensions and the restore path are all "
                    "PostgreSQL's; a SQLite file is copied, not dumped",
                },
            ).with_class(ErrorClass.INVALID_CONTENT)

        async with self._engine.connect() as connection:
            server = (await connection.execute(text("SHOW server_version"))).scalar_one()
            extensions = {
                name: version
                for name, version in (
                    await connection.execute(
                        text("SELECT extname, extversion FROM pg_extension ORDER BY extname")
                    )
                ).all()
                # `plpgsql` is in every database and tells a restore nothing.
                if name != "plpgsql"
            }
            revision = await self._schema_revision(connection)
            relations = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN "
                        "('pg_catalog', 'information_schema')"
                    )
                )
            ).scalar_one()
        return DatabaseFingerprint(
            server_version=str(server),
            extensions=extensions,
            schema_revision=revision,
            relation_count=int(relations),
        )

    async def _schema_revision(self, connection) -> str | None:
        """The stamped Alembic revision, or `None` if the table is absent.

        Absent is a real state, not an error: `lab db create` builds the schema
        without Alembic, and a backup of such a database is restorable as long
        as somebody knows to stamp it afterwards. Reporting it as a failure
        would make a legitimate scratch installation unbackupable.
        """
        exists = (
            await connection.execute(text("SELECT to_regclass('public.alembic_version')"))
        ).scalar()
        if exists is None:
            return None
        rows = (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
        if not rows:
            return None
        if len(rows) > 1:
            # Multiple heads. Restorable, but which revision the dump is at is
            # no longer a single answer, so say so rather than pick one.
            return ",".join(sorted(str(row[0]) for row in rows))
        return str(rows[0][0])

    # ── taking the dump ────────────────────────────────────────────────────

    async def dump(self, destination: Path) -> DumpResult:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        parameters = connection_parameters(self._url)
        tool_version = await self._tool_version(self._pg_dump)

        await self._run(
            self._pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--host={parameters['host']}",
            f"--port={parameters['port']}",
            f"--username={parameters['user']}",
            f"--dbname={parameters['dbname']}",
            f"--file={destination}",
            password=parameters.get("password"),
            what="pg_dump",
        )

        if not destination.is_file():  # pragma: no cover - pg_dump would have failed
            raise LabAssistantError(
                "pg_dump reported success and wrote no file",
                context={"destination": str(destination)},
            ).with_class(ErrorClass.INTERNAL)

        return DumpResult(
            path=str(destination),
            byte_length=destination.stat().st_size,
            sha256=_digest_file(destination),
            tool_version=tool_version,
        )

    async def restore(self, source: Path, *, clean: bool = False) -> None:
        source = Path(source)
        if not source.is_file():
            raise LabAssistantError(
                "there is no dump at that path",
                context={"source": str(source)},
            ).with_class(ErrorClass.NOT_FOUND)
        parameters = connection_parameters(self._url)
        arguments = [
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            f"--host={parameters['host']}",
            f"--port={parameters['port']}",
            f"--username={parameters['user']}",
            f"--dbname={parameters['dbname']}",
        ]
        if clean:
            arguments += ["--clean", "--if-exists"]
        await self._run(
            self._pg_restore,
            *arguments,
            str(source),
            password=parameters.get("password"),
            what="pg_restore",
        )

    # ── running the external tool ──────────────────────────────────────────

    async def _tool_version(self, program: str) -> str:
        if shutil.which(program) is None:
            raise LabAssistantError(
                f"{program} is not on PATH",
                context={
                    "why": "a portable backup is a logical dump, and the dump is taken by "
                    f"{program}",
                    "fix": "install the PostgreSQL client tools, or run the command in the "
                    "api container, which has them",
                },
            ).with_class(ErrorClass.INVALID_CONTENT)
        process = await asyncio.create_subprocess_exec(
            program,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await process.communicate()
        return out.decode("utf-8", "replace").strip()

    async def _run(self, program: str, *arguments: str, password: str | None, what: str) -> None:
        environment = dict(os.environ)
        if password is not None:
            # Here, and never in `arguments`: a command line is visible to every
            # process on the host and lands in shell history.
            environment["PGPASSWORD"] = password
        process = await asyncio.create_subprocess_exec(
            program,
            *arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            raise LabAssistantError(
                f"{what} did not finish within {self._timeout:.0f}s",
                context={"hint": "a large corpus needs a longer timeout, not a retry"},
            ).with_class(ErrorClass.TIMEOUT) from None
        if process.returncode != 0:
            raise LabAssistantError(
                f"{what} failed",
                context={
                    "returncode": process.returncode,
                    # The tool's own message, which names the real problem. It
                    # does not contain the password: that went through the
                    # environment.
                    "stderr": stderr.decode("utf-8", "replace").strip()[-2000:],
                },
            ).with_class(ErrorClass.INTERNAL)


def _digest_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 of a file, read in chunks.

    A dump of a real corpus does not fit in memory, and the one place a backup
    must not fail for want of RAM is while it is recording what it just wrote.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
