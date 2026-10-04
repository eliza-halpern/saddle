"""Compaction in a chat that looks at the screen.

Each screenshot reaches the model as a user-role message ("Image from
read_file call ..."), so a chat that acts on windows gains one user message
per action. Live (LibreOffice run, minute 6): compaction pinned the latest
of those as "the question being answered" and dropped the person's task,
along with the command output that held the working folder's path; the
model's next action typed an invented folder into the Save dialog.

Contracts: the pinned message in a chat is the person's latest message,
never an image follow-up; and old screenshots, stale once newer ones exist,
are elided to a text line before whole exchanges are dropped.
"""

from __future__ import annotations

from typing import Any

from saddle.memory import IMAGE_ELIDED, IMAGE_TOKENS, KEEP_RECENT, compact, pinned_index
from saddle.vision import images_message

TASK = "TASK-7c2: type a title in the Writer window and save it as owls.odt in this folder"
PNG = "data:image/png;base64,iVBORw0KGgo="


def _chat(rounds: int, *, reply: str = "ok") -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are careful."},
        {"role": "user", "content": TASK},
    ]
    for n in range(rounds):
        call_id = f"call_{n}"
        messages += [
            {
                "role": "assistant",
                "content": reply,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "computer", "arguments": '{"action":"key"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "done: press Return"},
            images_message([(call_id, f"screenshot {n}", PNG)]),
        ]
    return messages


def _images(messages: list[dict[str, Any]]) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if part.get("type") == "image_url"
    )


def test_the_person_s_message_is_pinned_not_a_screenshot() -> None:
    messages = _chat(3)
    assert messages[pinned_index(messages, "last")]["content"] == TASK
    messages.append({"role": "user", "content": "now bold it"})
    assert messages[pinned_index(messages, "last")]["content"] == "now bold it"


def test_the_task_survives_compaction_of_a_screen_chat() -> None:
    """Known-bad (live): the task was dropped and the latest screenshot kept as
    the pin. Known-good: whatever has to go, the task stays verbatim."""
    messages = _chat(12, reply="x" * 4000)  # text heavy enough that dropping is needed
    dropped, _ = compact(messages, limit_tokens=4000)
    assert dropped > 0
    assert {"role": "user", "content": TASK} in messages


def test_old_screenshots_go_before_any_exchange_does() -> None:
    """Twelve screenshots outgrow a 5,000-token window; with the older ones
    elided to a line, the conversation fits and nothing is dropped."""
    messages = _chat(12)
    assert _images(messages) * IMAGE_TOKENS > 5000
    dropped, summary = compact(messages, limit_tokens=5000)
    assert dropped == 0
    assert summary == "older screenshots elided"
    kept = _images(messages)
    assert 1 <= kept <= KEEP_RECENT // 3 + 1  # the recent tail keeps its pictures
    assert sum(IMAGE_ELIDED in str(m.get("content")) for m in messages) == 12 - kept
    assert {"role": "user", "content": TASK} in messages


def test_a_picture_the_person_attached_is_never_elided() -> None:
    attached: dict[str, Any] = {
        "role": "user",
        "content": [
            {"type": "text", "text": "this is the error I see"},
            {"type": "image_url", "image_url": {"url": PNG}},
        ],
    }
    messages = _chat(0)
    messages.insert(1, attached)
    messages += _chat(12)[2:]
    compact(messages, limit_tokens=5000)
    assert attached in messages
    assert attached["content"][1]["type"] == "image_url"
