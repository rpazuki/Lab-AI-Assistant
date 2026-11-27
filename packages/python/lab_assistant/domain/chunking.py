"""The versioned chunker: a projection over rendition nodes.

Chunks are not the canonical text. Their identity is derived from the release
item, rendition digest, node set, chunker profile, and content digest — which is
exactly what makes an incremental release build possible, because an unchanged
item's chunks can be copied by digest instead of recomputed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from lab_assistant.contracts.canonical import fingerprint, sha256_text
from lab_assistant.contracts.parsing import NodeKind, RenditionDocument, RenditionNode

__all__ = [
    "CHUNKER_ID",
    "CHUNKER_VERSION",
    "Chunk",
    "ChunkerProfile",
    "chunk_rendition",
]

CHUNKER_ID = "structural-v1"
CHUNKER_VERSION = "1"

#: Rough characters-per-token ratio for English scientific prose. Used only for
#: budgeting, never for anything a metric depends on.
_CHARS_PER_TOKEN = 4


class ChunkerProfile(BaseModel):
    model_config = {"frozen": True}

    chunker_id: str = CHUNKER_ID
    chunker_version: str = CHUNKER_VERSION
    #: Target size in tokens; sections are split at sentence boundaries.
    target_tokens: int = 512
    overlap_tokens: int = 64
    #: Table rows are emitted as their own chunks so a cell stays citable.
    tables_as_chunks: bool = True
    include_captions: bool = True
    #: Reference lists are excluded: they are not focal content.
    include_references: bool = False

    @property
    def logical_key(self) -> str:
        return f"{self.chunker_id}@{self.chunker_version}"


class Chunk(BaseModel):
    """One projection row. ``chunk_key`` is stable across rebuilds."""

    model_config = {"frozen": True}

    chunk_key: str
    node_ids: tuple[str, ...]
    content: str
    content_sha256: str
    token_count: int
    chunk_type: str
    section_label: str | None = None
    page: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _split_sentences(text: str) -> list[str]:
    """Sentence-ish split that never breaks mid-word.

    Deliberately simple: a chunk boundary only needs to be deterministic and not
    mid-sentence. Anything cleverer would need its own versioning story.
    """
    out: list[str] = []
    buffer: list[str] = []
    for index, char in enumerate(text):
        buffer.append(char)
        if char in ".!?":
            following = text[index + 1 : index + 2]
            if following in ("", " ", "\n"):
                out.append("".join(buffer).strip())
                buffer = []
    tail = "".join(buffer).strip()
    if tail:
        out.append(tail)
    return out or [text.strip()]


def _pack(sentences: Sequence[str], *, target_tokens: int, overlap_tokens: int) -> list[str]:
    windows: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        sentence_tokens = _estimate_tokens(sentence)
        if current and current_tokens + sentence_tokens > target_tokens:
            windows.append(" ".join(current))
            # Overlap by whole sentences from the tail of the previous window.
            overlap: list[str] = []
            overlap_count = 0
            for previous in reversed(current):
                previous_tokens = _estimate_tokens(previous)
                if overlap_count + previous_tokens > overlap_tokens:
                    break
                overlap.insert(0, previous)
                overlap_count += previous_tokens
            current = [*overlap, sentence]
            current_tokens = overlap_count + sentence_tokens
        else:
            current.append(sentence)
            current_tokens += sentence_tokens
    if current:
        windows.append(" ".join(current))
    return windows


def _chunk_key(
    *,
    release_item_digest: str,
    rendition_digest: str,
    profile: ChunkerProfile,
    node_ids: Iterable[str],
    content_digest: str,
) -> str:
    return fingerprint(
        kind="chunk",
        parts={
            "release_item_digest": release_item_digest,
            "rendition_digest": rendition_digest,
            "chunker": profile.logical_key,
            "node_ids": list(node_ids),
            "content_digest": content_digest,
        },
    )


def _storable(text: str) -> str:
    """The text without NUL characters, which Postgres refuses to store as text.

    PDF text extraction emits them where a glyph has no Unicode mapping (often a
    ligature, as in "classi\x00cation"). One such character failed a whole release's
    index build, so every chunk passes through here, whichever parser made it.
    """
    return text.replace("\x00", "")


def _section_context(node: RenditionNode, ancestry: Sequence[RenditionNode]) -> str | None:
    for parent in reversed(ancestry):
        if parent.kind in (NodeKind.SECTION, NodeKind.ABSTRACT, NodeKind.SHEET):
            label = parent.label or parent.title
            return _storable(label) if label else label
    label = node.label or node.title
    return _storable(label) if label else label


def chunk_rendition(
    rendition: RenditionDocument,
    *,
    release_item_digest: str,
    rendition_digest: str,
    profile: ChunkerProfile | None = None,
) -> tuple[Chunk, ...]:
    """Project a rendition into deterministic, citable chunks."""
    profile = profile or ChunkerProfile()
    chunks: list[Chunk] = []

    def visit(node: RenditionNode, ancestry: list[RenditionNode]) -> None:
        if not profile.include_references and not node.is_focal:
            return
        if node.kind is NodeKind.TABLE and profile.tables_as_chunks:
            _emit_table(node, ancestry)
        elif (
            node.kind
            in (
                NodeKind.PARAGRAPH,
                NodeKind.ABSTRACT,
                NodeKind.TITLE,
                NodeKind.LIST_ITEM,
            )
            and node.text.strip()
        ) or (
            node.kind in (NodeKind.FIGURE_CAPTION, NodeKind.TABLE_CAPTION)
            and profile.include_captions
            and node.text.strip()
        ):
            _emit_text(node, ancestry)
        for child in node.children:
            visit(child, [*ancestry, node])

    def _emit_text(node: RenditionNode, ancestry: list[RenditionNode]) -> None:
        windows = _pack(
            _split_sentences(node.text),
            target_tokens=profile.target_tokens,
            overlap_tokens=profile.overlap_tokens,
        )
        for window in windows:
            content = _storable(window).strip()
            if not content:
                continue
            digest = sha256_text(content)
            chunks.append(
                Chunk(
                    chunk_key=_chunk_key(
                        release_item_digest=release_item_digest,
                        rendition_digest=rendition_digest,
                        profile=profile,
                        node_ids=[node.node_id],
                        content_digest=digest,
                    ),
                    node_ids=(node.node_id,),
                    content=content,
                    content_sha256=digest,
                    token_count=_estimate_tokens(content),
                    chunk_type=node.kind.value,
                    section_label=_section_context(node, ancestry),
                    page=node.coordinates.page if node.coordinates else None,
                )
            )

    def _emit_table(node: RenditionNode, ancestry: list[RenditionNode]) -> None:
        header_cells = [cell for cell in node.cells if cell.is_header]
        headers = {cell.column: cell.text for cell in header_cells}
        rows: dict[int, list[str]] = {}
        row_nodes: dict[int, list[str]] = {}
        for cell in node.cells:
            if cell.is_header:
                continue
            label = headers.get(cell.column)
            rendered = f"{label}: {cell.text}" if label else cell.text
            rows.setdefault(cell.row, []).append(rendered)
            row_nodes.setdefault(cell.row, []).append(cell.node_id)
        caption = next(
            (child.text for child in node.children if child.kind is NodeKind.TABLE_CAPTION),
            node.title or node.label or "",
        )
        for row_index in sorted(rows):
            content = _storable(" | ".join(rows[row_index])).strip()
            if not content:
                continue
            if caption:
                content = f"{_storable(caption)} — {content}"
            digest = sha256_text(content)
            node_ids = tuple(sorted(row_nodes[row_index]))
            chunks.append(
                Chunk(
                    chunk_key=_chunk_key(
                        release_item_digest=release_item_digest,
                        rendition_digest=rendition_digest,
                        profile=profile,
                        node_ids=node_ids,
                        content_digest=digest,
                    ),
                    node_ids=node_ids,
                    content=content,
                    content_sha256=digest,
                    token_count=_estimate_tokens(content),
                    chunk_type="table_row",
                    section_label=_section_context(node, ancestry),
                    page=node.coordinates.page if node.coordinates else None,
                    metadata={"table_node_id": node.node_id, "row": row_index},
                )
            )

    for top in rendition.nodes:
        visit(top, [])
    return _distinct(chunks)


def _distinct(chunks: Sequence[Chunk]) -> tuple[Chunk, ...]:
    """Each chunk once, in order.

    A passage repeated within one node (text a PDF prints twice, a sentence
    repeated until it fills a window) packs into identical windows, and identical
    windows of one node have one key. The index stores a key once, so a second
    copy would only be counted: one such pair failed a release's membership check
    on the deployment, 236,729 chunks written against 236,728 stored.
    """
    seen: set[str] = set()
    unique: list[Chunk] = []
    for chunk in chunks:
        if chunk.chunk_key not in seen:
            seen.add(chunk.chunk_key)
            unique.append(chunk)
    return tuple(unique)
