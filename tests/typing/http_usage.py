"""`shared.http`'s types, checked by pyright and never run. Each `pyright: ignore` line must stay an error."""

from typing import Optional

from typing_extensions import assert_type

from shared import http

assert_type(http.fetch("https://example.com/"), http.Response)
assert_type(http.fetch("https://example.com/", soft=False), http.Response)
assert_type(http.fetch("https://example.com/", soft=True), Optional[http.Response])


def soft_flag(soft: bool) -> None:
    assert_type(http.fetch("https://example.com/", soft=soft), Optional[http.Response])


# `soft=True` may give None, so the caller has to handle it before reading a field.
http.fetch("https://example.com/", soft=True).status  # pyright: ignore[reportOptionalMemberAccess]
http.fetch("https://example.com/", "POST")  # pyright: ignore[reportCallIssue]
