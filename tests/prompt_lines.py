"""Line-based answers for tests that drive prompts without a real TTY."""

from __future__ import annotations

from unittest import mock


def select_from_lines(*lines: str) -> mock._patch:
    """Mock questionary prompts to take each answer from `lines`, in order."""
    pending = list(lines)

    def take() -> str | None:
        return pending.pop(0) if pending else None

    def select(_message, choices, **_kwargs):
        def unsafe_ask() -> object | None:
            while pending:
                pick = pending.pop(0).strip()
                if pick.isdigit():
                    index = int(pick) - 1
                    if 0 <= index < len(choices):
                        return choices[index].value
                    continue
                for item in choices:
                    if pick == str(item.title) or pick == str(item.value):
                        return item.value
            return None

        return mock.Mock(unsafe_ask=unsafe_ask)

    def text(_message, **kwargs):
        return mock.Mock(unsafe_ask=lambda: take() or kwargs.get("default") or "")

    def password(_message, **_kwargs):
        return mock.Mock(unsafe_ask=lambda: take() or "")

    return mock.patch.multiple(
        "flncore.prompt.questionary",
        select=mock.Mock(side_effect=select),
        text=mock.Mock(side_effect=text),
        password=mock.Mock(side_effect=password),
    )