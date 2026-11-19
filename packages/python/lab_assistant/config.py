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
from uuid import UUID

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lab_assistant.contracts.canonical import sha256_text
from lab_assistant.domain.installation import InstallationRole

__all__ = [
    "ConfigProvenance",
    "LabEnv",
    "Settings",
    "load_settings",
]

_SECRET_KEYS = frozenset(
    {
        "secret_key",
        "archive_signing_key_path",
        "database_url",
        "postgres_password",
        "llm_api_key",
        "object_store_secret_key",
        "object_store_access_key",
        "object_store_session_token",
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

    # ── installation identity ───────────────────────────────────────────────
    # Who this installation is, as distinct from which library or workspace it
    # serves. It exists for one rule: a backup is a faithful copy, so restoring
    # one always produces something that believes it is the publisher. The
    # recovery manifest records this id, and `domain/recovery.py` refuses a
    # restore that would quietly stand a second publisher beside the first.
    #
    # Unset is supported and is not a failure: the identity is then derived from
    # the installation's own addresses, and the manifest says `derived` rather
    # than `configured`. A derived identity changes when the installation moves,
    # which is the right default — a clone on a new host is a new installation —
    # but it cannot survive a deliberate address change, so a deployment that
    # means to keep its identity across a move sets this before moving.
    installation_id: UUID | None = None
    #: What this deployment is for: `publisher` curates and publishes the
    #: corpus, `reader` serves an already-approved one. It is not a user role
    #: and it is not read-only — a reader writes logins, chat, feedback and
    #: audit constantly. What it must not do is change the corpus.
    #:
    #: Defaults to `publisher` so an installation that predates the setting
    #: keeps every capability it had. See `domain/installation.py`.
    installation_role: InstallationRole = InstallationRole.PUBLISHER
    #: Incremented when a fenced handover transfers the right to publish. Phase 4
    #: makes it durable; today it is configuration, recorded so a backup taken
    #: now is readable by the implementation that does.
    writer_epoch: int = 0

    # ── archive signing ─────────────────────────────────────────────────────
    # A digest proves the bytes did not rot; a signature establishes who
    # approved them. Anyone who can write to the bucket can write a correct
    # digest, so bucket administration is not evidence that a steward reviewed
    # a claim.
    #
    # The private key is a path rather than a value: an environment variable
    # reaches process listings, crash dumps and support bundles, and a signing
    # key that has been in one is a key to rotate.
    archive_signing_key_path: Path | None = None
    archive_signing_key_id: str | None = None
    #: Public keys this deployment trusts, as `id=base64` entries. Pinned in
    #: configuration and never resolved from the archive: a key fetched from
    #: beside the manifest is a key the manifest's author chose. Rotation means
    #: an operator adds the new id here, which is an authenticated transition
    #: because changing configuration is one.
    archive_trusted_keys: dict[str, str] = Field(default_factory=dict)
    #: Refuse a snapshot that is unsigned or signed by an unknown key. Off by
    #: default so signing can be adopted without making everything published
    #: before it unreadable; a bad signature always blocks regardless.
    archive_require_signature: bool = False

    # ── archive policy checkpoints ──────────────────────────────────────────
    # How long a checkpoint this publisher issues may be relied on. A reader
    # that cannot refresh policy stops serving restricted material when the
    # window closes, so this is the bound on how long a revocation can go
    # unseen — and the publisher has to re-issue inside it, on a timer, or
    # readers stop serving restricted material for want of a fresh checkpoint.
    archive_policy_validity_hours: int = Field(default=24, gt=0)
    #: An offline window for *unrestricted* material, longer than the one
    #: above. 0 means none: public material is held to the same bound. It is
    #: written into the signed checkpoint, so it is the publisher's steward who
    #: approves it, not each reader's operator.
    archive_policy_public_validity_hours: int = Field(default=0, ge=0)
    #: Whether open-licence content (CC and public-domain) may be *replicated*:
    #: copied into the shared archive and onto the readers that import it. A
    #: data-steward decision, off by default, because the architecture requires
    #: an explicit permission for replication and denial while it is unmade —
    #: `store_content` here is not permission to store it on another machine.
    #: Applies to rights decisions made after it is set; earlier decisions keep
    #: the actions they were granted.
    rights_replicate_open_content: bool = False

    # ── database ────────────────────────────────────────────────────────────
    # Owner of this address: Settings (local) and the compose `environment:`
    # block (compose/server). Never restated in an address file with a password.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/lab_assistant"
    database_pool_size: int = 10
    database_statement_timeout_ms: int = 30_000

    # ── object store ────────────────────────────────────────────────────────
    allow_filesystem_in_production: bool = False
    object_store_kind: str = "filesystem"  # filesystem | s3 | gcs
    #: GCS only. The project the bucket belongs to; the application-default
    #: credential usually carries one, and this overrides it.
    object_store_project: str | None = None
    #: GCS only. A service-account JSON file, for a deployment that cannot use
    #: workload identity. Prefer workload identity: a key file is a credential
    #: that does not expire and has to be rotated by somebody remembering to.
    object_store_credentials_path: str | None = None
    object_store_root: Path = Path("./var/objects")
    object_store_bucket: str | None = None
    object_store_endpoint: str | None = None
    object_store_region: str | None = None
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = None
    #: Temporary credentials. Leaving all three unset selects the SDK credential
    #: chain, which is how a deployment gets a role whose session refreshes
    #: inside a job longer than the session's lifetime (ADR-0023).
    object_store_session_token: str | None = None
    object_store_profile: str | None = None
    #: Shared-bucket isolation. Prepended to every key and never stored on a
    #: record, so the database stays portable across buckets and prefixes.
    #: Changing it on a populated bucket is a migration, not a setting change.
    object_store_prefix: str = ""
    #: Off only for an endpoint that rejects preconditions, which reopens the
    #: write race conditional creates close.
    object_store_conditional_writes: bool = True
    #: Refuse a payload larger than this, while reading rather than after. 0
    #: means no store-level limit; a caller's own limit still applies.
    object_store_max_object_bytes: int = 0
    #: At or above this, an upload goes in parts, so a failed transfer resumes
    #: from a part boundary rather than from zero. 8 MiB matches the SDK's own
    #: default and the spool threshold, so a payload small enough to send in one
    #: request is also small enough never to touch disk.
    object_store_multipart_threshold_bytes: int = 8 * 1024 * 1024

    # ── auth ────────────────────────────────────────────────────────────────
    secret_key: str = "development-only-not-for-production"
    access_token_ttl_minutes: int = 480
    password_hash_scheme: str = "argon2id"
    login_rate_limit_per_minute: int = 10

    # ── chat allowance and oversight ────────────────────────────────────────
    #: Tokens (question and answer, summed over every answer) a member may use,
    #: unless an admin sets their own limit. The predecessor's default.
    chat_token_limit_default: int = 1_000_000
    #: Whether admins may read members' conversations. When on, members are told
    #: so on the Ask page, and every read is audited.
    chat_admin_access: bool = True

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

    @field_validator("archive_trusted_keys", mode="before")
    @classmethod
    def _split_trusted_keys(cls, value: Any) -> Any:
        """Accept `id=base64,id2=base64` so a deployment can pin keys in one
        environment entry without a config file."""
        if not isinstance(value, str):
            return value
        pairs: dict[str, str] = {}
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            identifier, _, key = entry.partition("=")
            if not identifier or not key:
                raise ValueError(
                    "LAB_ARCHIVE_TRUSTED_KEYS entries are 'id=base64', separated by commas"
                )
            pairs[identifier.strip()] = key.strip()
        return pairs

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
        if self.object_store_kind == "filesystem" and not self.allow_filesystem_in_production:
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
