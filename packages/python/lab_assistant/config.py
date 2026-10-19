"""Deployment configuration.

One variable names the scenario; everything else derives from it::

    LAB_ENV = local | compose | server

This carries forward a lesson the predecessor learned the hard way, and which
the repository plan's layered loading order would otherwise have discarded (see
``docs/architecture/adr/0012-configuration-model.md``):

* ``.env`` holds **secrets and behaviour only — no hostnames, ports, or URLs**,
  so ``source .env`` is safe in any shell;
* addresses live in per-scenario committed files (``.env.compose``,
  ``.env.server``) injected by the compose file that owns them;
* **each address has exactly one owner**. The database URL is owned by
  ``Settings`` and by the ``environment:`` block the compose file builds from
  ``POSTGRES_*``; nothing else restates it;
* **no password is duplicated into a ``DATABASE_URL``** in an address file.

Defaults serve ``local``, because a bare shell has no injection mechanism while
compose always injects addresses explicitly. That is why ``database_url``
defaults to ``localhost:5433`` and never to ``db:5432``.

Loading order (lowest precedence first):

1. compiled defaults (this file);
2. versioned deployment config file ``infra/environments/<LAB_ENV>.yaml``
   (behaviour only, never secrets);
3. workspace config records in the database (per-workspace scientific settings —
   *not* handled here; they are ``ConfigVersion`` rows);
4. environment variables, for deployment endpoints and secrets;
5. explicit CLI flags, for one invocation.

``lab config print --redacted`` shows every key with its source and a checksum,
and never a secret value.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lab_assistant.contracts.canonical import sha256_text

__all__ = [
    "ConfigProvenance",
    "LabEnv",
    "Settings",
    "load_settings",
]

_SECRET_KEYS = frozenset(
    {
        "secret_key",
        "database_url",
        "postgres_password",
        "llm_api_key",
        "object_store_secret_key",
        "object_store_access_key",
        "ncbi_api_key",
        "unpaywall_email",
        "smtp_password",
    }
)


class LabEnv(StrEnum):
    LOCAL = "local"
    COMPOSE = "compose"
    SERVER = "server"


class ConfigProvenance:
    """Where each effective value came from, for ``--print-effective-config``."""

    def __init__(self) -> None:
        self._sources: dict[str, str] = {}

    def record(self, key: str, source: str) -> None:
        self._sources[key] = source

    def source_of(self, key: str) -> str:
        return self._sources.get(key, "compiled default")

    def as_dict(self) -> dict[str, str]:
        return dict(sorted(self._sources.items()))


class Settings(BaseSettings):
    """Effective deployment settings.

    The backend is environment-variable driven by design and has no YAML config
    file of its own beyond the committed per-scenario behaviour file.
    """

    model_config = SettingsConfigDict(
        env_prefix="LAB_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    env: LabEnv = LabEnv.LOCAL

    # ── database ────────────────────────────────────────────────────────────
    # Owner of this address: Settings (local) and the compose `environment:`
    # block (compose/server). Never restated in an address file with a password.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/lab_assistant"
    database_pool_size: int = 10
    database_statement_timeout_ms: int = 30_000

    # ── object store ────────────────────────────────────────────────────────
    object_store_kind: str = "filesystem"  # filesystem | s3
    object_store_root: Path = Path("./var/objects")
    object_store_bucket: str | None = None
    object_store_endpoint: str | None = None
    object_store_region: str | None = None
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = None

    # ── auth ────────────────────────────────────────────────────────────────
    secret_key: str = "development-only-not-for-production"
    access_token_ttl_minutes: int = 480
    password_hash_scheme: str = "argon2id"
    login_rate_limit_per_minute: int = 10

    # ── model providers ─────────────────────────────────────────────────────
    llm_provider: str = "fake"  # fake | anthropic
    llm_model: str = "claude-sonnet-4-6"
    llm_api_key: str | None = None
    llm_max_output_tokens: int = 2048
    llm_temperature: float = 0.1
    external_model_enabled: bool = False

    embedding_profile: str = "hashing-v1"  # hashing-v1 | sentence-transformers
    embedding_batch_size: int = 32
    embedding_model_cache: Path = Path("./var/model-cache")

    # ── retrieval ───────────────────────────────────────────────────────────
    retrieval_lexical_top_k: int = 20
    retrieval_vector_top_k: int = 20
    retrieval_final_top_k: int = 8
    reranker_enabled: bool = False
    reranker_model: str | None = None

    # ── connectors ──────────────────────────────────────────────────────────
    connector_request_identity: str = "lab-ai-assistant (configure LAB_CONNECTOR_CONTACT)"
    connector_contact: str | None = None
    ncbi_api_key: str | None = None
    live_source_tests_enabled: bool = False

    # ── staged controls (scale §3.2) ────────────────────────────────────────
    # Each of these is a real control that starts at a proportionate depth. The
    # flag changes enforcement depth, never the schema, and the enabled path's
    # tests exercise the disabled path's dependencies.
    row_level_security_enabled: bool = False
    malware_scan_required_for_uploads: bool = True
    malware_scan_required_for_open_access: bool = False
    parser_sandbox_strict: bool = False
    double_review_enabled: bool = False
    tracing_enabled: bool = False

    # ── operations ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    app_public_url: str = "http://localhost:3000"
    worker_lease_seconds: int = 300
    worker_poll_interval_seconds: float = 1.0
    #: Per-stage concurrency caps. This, not the number of worker deployments, is
    #: what stops a long provider call starving another queue.
    stage_concurrency: dict[str, int] = Field(
        default_factory=lambda: {
            "discovery": 4,
            "acquisition": 4,
            "parsing": 2,
            "extraction": 2,
            "indexing": 1,
            "evaluation": 1,
        }
    )
    monthly_token_budget: int = 5_000_000

    #: Keys whose values must never be printed or logged.
    secret_field_names: ClassVar[frozenset[str]] = _SECRET_KEYS

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                import json

                return tuple(json.loads(stripped))
            return tuple(part.strip() for part in stripped.split(",") if part.strip())
        return value

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            # A sync URL in an async application fails at connect time with a
            # confusing error; correct it here instead.
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def is_production(self) -> bool:
        return self.env is LabEnv.SERVER

    @property
    def is_containerised(self) -> bool:
        return self.env in (LabEnv.COMPOSE, LabEnv.SERVER)

    @property
    def sync_database_url(self) -> str:
        """Alembic and psql-style tooling need the sync driver."""
        return self.database_url.replace("+asyncpg", "")

    def production_blockers(self) -> tuple[str, ...]:
        """Settings that must not reach a production deployment."""
        blockers: list[str] = []
        if not self.is_production:
            return ()
        if self.secret_key == "development-only-not-for-production":
            blockers.append("SECRET_KEY is still the development default")
        if "localhost" in self.app_public_url:
            blockers.append("APP_PUBLIC_URL points at localhost")
        if any("localhost" in origin for origin in self.cors_origins):
            blockers.append("CORS_ORIGINS still contains localhost")
        if self.llm_provider == "fake":
            blockers.append("LLM_PROVIDER is the deterministic fake")
        if self.llm_provider != "anthropic":
            blockers.append("LLM_PROVIDER must be anthropic in server mode")
        elif not self.llm_api_key:
            blockers.append("LLM_API_KEY is missing")
        if self.object_store_kind == "filesystem":
            blockers.append(
                "object store is a local directory; a directory on the database disk is "
                "not a durable data plane (quality §9.1)"
            )
        return tuple(blockers)

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        for key in self.secret_field_names:
            if key in data and data[key] not in (None, ""):
                data[key] = f"[redacted sha256:{sha256_text(str(data[key]))[:12]}]"
        return data


def _environment_file(env: LabEnv, *, root: Path) -> Path:
    return root / "infra" / "environments" / f"{env.value}.yaml"


def load_settings(
    *,
    root: Path | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[Settings, ConfigProvenance]:
    """Assemble effective settings and record where each value came from."""
    environ = dict(os.environ if environ is None else environ)
    root = root or Path.cwd()
    provenance = ConfigProvenance()

    env_name = environ.get("LAB_ENV", LabEnv.LOCAL.value).lower()
    try:
        env = LabEnv(env_name)
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(
            f"LAB_ENV={env_name!r} is not one of: {', '.join(e.value for e in LabEnv)}"
        ) from exc

    values: dict[str, Any] = {"env": env}
    provenance.record("env", f"environment variable LAB_ENV={env.value}")

    config_path = _environment_file(env, root=root)
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a mapping")
        for key, value in loaded.items():
            normalised = key.lower()
            if normalised in _SECRET_KEYS:
                raise ValueError(
                    f"{config_path} contains secret key {key!r}; secrets belong in the "
                    "environment or a secret manager, never in a committed file"
                )
            values[normalised] = value
            provenance.record(normalised, f"{config_path.name}")

    for field_name in Settings.model_fields:
        env_key = f"LAB_{field_name.upper()}"
        if env_key in environ:
            values[field_name] = environ[env_key]
            provenance.record(field_name, f"environment variable {env_key}")

    for key, value in (overrides or {}).items():
        values[key] = value
        provenance.record(key, "command-line flag")

    settings = Settings(**values)
    return settings, provenance
