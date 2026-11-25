"""An answer resting on an abstract says so: the prompt marks each such block."""

from __future__ import annotations

from lab_assistant.adapters.models.anthropic_provider import _system_blocks
from lab_assistant.application.chat import ANSWER_PROMPT
from lab_assistant.contracts.models import EvidenceBlock, GenerationRequest


def test_an_abstract_block_is_marked_and_the_prompt_says_what_to_do() -> None:
    request = GenerationRequest(
        system_prompt=ANSWER_PROMPT,
        prompt_bundle_id="answer-researcher",
        prompt_bundle_version="2",
        question="What do honeybees need?",
        evidence=(
            EvidenceBlock(handle="E1", text="Full text finding.", source_label="Nature"),
            EvidenceBlock(handle="E2", text="Abstract finding.", abstract_only=True),
        ),
    )
    [block] = _system_blocks(request)
    assert "[E2] (abstract only) Abstract finding." in block["text"]
    assert "[E1] Full text finding." in block["text"]
    assert "abstract only" in ANSWER_PROMPT
