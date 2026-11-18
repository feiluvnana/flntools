"""Line-based answers for tests that drive prompts without a real terminal."""

from __future__ import annotations

from unittest import mock

from flnd.ui import forms


def select_from_lines(*lines: str) -> mock._patch:
    """Answer `forms.select` and `forms.text` from `lines`, in order.

    A pick is a number from 1 or a label or value; a line that is neither is skipped, as a wrong
    answer asked again. Out of lines, a question gets None, as esc gives.
    """
    pending = list(lines)

    def select(_title, options, **_kwargs):
        picks = forms._options(options)
        while pending:
            pick = pending.pop(0).strip()
            if pick.isdigit() and 0 < int(pick) <= len(picks):
                return picks[int(pick) - 1].value
            for item in picks:
                if pick in (item.label, str(item.value)):
                    return item.value
        return None

    def text(_title, *, default=None, **_kwargs):
        return (pending.pop(0) if pending else None) or default or ""

    return mock.patch.multiple(forms, select=mock.Mock(side_effect=select),
                               text=mock.Mock(side_effect=text))
