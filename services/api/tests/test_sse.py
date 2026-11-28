"""Server-sent events carry a token's line breaks intact.

An answer in Markdown has a line break at every paragraph and list item. Written
as one ``data:`` line, the blank line inside a token ended the event early, so the
live answer arrived as one run-on paragraph with pieces missing; it only looked
right once reloaded from the database.
"""

from __future__ import annotations

import json

from lab_assistant_api.routes import _sse


def _client_read(frame: str) -> tuple[str, str]:
    """What a conforming client makes of one event: its name and its data."""
    assert frame.endswith("\n\n")
    body = frame[:-2]
    assert "\n\n" not in body, "the event would end before its data does"
    event, data = "", []
    for line in body.split("\n"):
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data.append(line[len("data: ") :])
    return event, "\n".join(data)


def test_a_token_with_line_breaks_survives() -> None:
    token = "## Yield\n\n- strain YL-A: 0.42 g/g\r\n- strain YL-B"
    assert _client_read(_sse("token", token)) == (
        "token",
        "## Yield\n\n- strain YL-A: 0.42 g/g\n- strain YL-B",
    )


def test_a_plain_token_and_a_json_event_are_unchanged() -> None:
    assert _sse("token", " word") == "event: token\ndata:  word\n\n"
    event, data = _client_read(_sse("done", {"message_id": "m1"}))
    assert event == "done" and json.loads(data) == {"message_id": "m1"}
