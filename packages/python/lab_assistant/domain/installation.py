"""What a deployment of this application is allowed to do.

A *user* role says what a person may do inside a workspace. An *installation*
role says what a running deployment may do at all, and the two are unrelated:
an admin on a reader installation still cannot publish a release, because the
machine they are typing at is not the publisher. Conflating them is how a
restored backup quietly becomes a second writer (`domain/recovery.py` has the
other half of that story).

The split is **not** read-only versus read-write, and getting that wrong is the
trap. A reader writes constantly: login timestamps, rate-limit buckets, audit
events, chat sessions, messages, feedback, export scratch. Blocking writes — or
blocking POST, when search is itself a POST — would produce an installation
that cannot serve anybody. What a reader must not do is change *the corpus*:
acquire sources, parse them, extract claims, curate what is included, or decide
what is published. Those are the publisher's, and there is one publisher.

So capabilities name operations, not HTTP verbs and not database access.

Enforcement lives in four places, and the layering is deliberate rather than
redundant:

- **composition** — a reader builds no connectors, parsers, extractor or
  scanner, so the acquisition path has nothing to call even if it is reached.
  This is the strongest of the four, because it removes the capability instead
  of guarding it, and it is why a reader needs no source API keys;
- **the application services** — the irreversible operations check before they
  act, so the CLI and a worker are covered by the same rule as a route;
- **the API** — every mutating route declares the capability it needs, and a
  contract test fails when a new one does not;
- **task claims** — a reader claims no stage, so a queue inherited in a restored
  database does not start running on it.

A guard at one layer only would be a sentence in a docstring. The audit has a
table of those (F18).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "READER_CAPABILITIES",
    "InstallationCapability",
    "InstallationRole",
    "InstanceCapabilities",
    "capabilities_for",
]


class InstallationRole(StrEnum):
    """What this deployment is for."""

    #: Curates and publishes the corpus. Today's single-VM deployment, and the
    #: default: an installation that has not been told what it is keeps every
    #: capability it had before this setting existed, so adding the setting
    #: cannot silently disable a running deployment.
    PUBLISHER = "publisher"
    #: Serves an already-approved corpus and the interactions around it. It
    #: acquires nothing, processes nothing and publishes nothing.
    READER = "reader"
    #: Drained for a handover, a migration or a recovery: it serves nobody and
    #: claims no work. What a publisher becomes before its right to publish is
    #: transferred, so nothing it starts can outlive its authority
    #: (`domain/handover.py`). Operator commands still run; that is the point.
    MAINTENANCE = "maintenance"


class InstallationCapability(StrEnum):
    """One thing a deployment may do, named as an operation."""

    #: Search, retrieval, citations — reading the published corpus.
    SERVE_CORPUS = "serve_corpus"
    #: Chat sessions and messages, feedback, logins, audit. A reader's whole
    #: reason to exist, and all of it writes.
    RECORD_INTERACTION = "record_interaction"
    #: Export packages a member is entitled to, to private storage.
    PRIVATE_EXPORT = "private_export"
    #: Scope definition, relevance decisions, identity merges and links, review
    #: outcomes — deciding what the corpus contains.
    CURATE_CORPUS = "curate_corpus"
    #: Discovery runs, acquisition, assisted upload — fetching from outside.
    ACQUIRE_SOURCES = "acquire_sources"
    #: Parsing, extraction, indexing — turning acquired bytes into evidence.
    PROCESS_CONTENT = "process_content"
    #: Drafting, validating, approving, publishing, rolling back and retracting.
    #: The capability that must belong to exactly one installation at a time.
    PUBLISH_RELEASES = "publish_releases"
    #: Claiming durable tasks. A reader runs no workers at all, so this is both
    #: a capability and a startup validation.
    RUN_WORKERS = "run_workers"


#: Everything a reader may do. Written as the allow-list rather than as a set of
#: exclusions: a capability added later is denied to readers until somebody
#: decides otherwise, which is the safe direction for a new operation.
READER_CAPABILITIES: frozenset[InstallationCapability] = frozenset(
    {
        InstallationCapability.SERVE_CORPUS,
        InstallationCapability.RECORD_INTERACTION,
        InstallationCapability.PRIVATE_EXPORT,
    }
)

_BY_ROLE: dict[InstallationRole, frozenset[InstallationCapability]] = {
    InstallationRole.PUBLISHER: frozenset(InstallationCapability),
    InstallationRole.READER: READER_CAPABILITIES,
    InstallationRole.MAINTENANCE: frozenset(),
}


def capabilities_for(role: InstallationRole) -> frozenset[InstallationCapability]:
    return _BY_ROLE[role]


@dataclass(frozen=True, slots=True)
class InstanceCapabilities:
    """The role this process is running under, and what it permits.

    Built once at composition and carried on the service context, so every
    layer consults the same object rather than re-reading configuration — and
    so a test can run a reader without an environment variable.
    """

    role: InstallationRole = InstallationRole.PUBLISHER

    @property
    def capabilities(self) -> frozenset[InstallationCapability]:
        return capabilities_for(self.role)

    def has(self, capability: InstallationCapability) -> bool:
        return capability in self.capabilities

    @property
    def is_publisher(self) -> bool:
        return self.role is InstallationRole.PUBLISHER

    def refuse_worker_startup(self) -> str | None:
        """Why this installation must not start a worker process, if it must not.

        Whether workers run is a deployment decision — you start the container
        or you do not — rather than a setting, so this is checked where a worker
        actually starts rather than inferred from configuration that is present
        in every profile.

        It matters most in the case that looks harmless: a reader restored from
        a publisher's backup inherits a queue full of ready work. Without this
        it would start acquiring sources and publishing as the installation it
        was copied from, and nothing about that looks like a failure.
        """
        if self.has(InstallationCapability.RUN_WORKERS):
            return None
        return (
            f"this installation is a {self.role.value} and runs no workers; "
            "its queue belongs to the publisher"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "capabilities": sorted(capability.value for capability in self.capabilities),
        }
