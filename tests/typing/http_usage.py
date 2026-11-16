"""`flnd._http`'s types, checked by pyright and never run. Each `pyright: ignore` line must stay an error."""

from typing_extensions import assert_type

from flnd import _http as http

assert_type(http.fetch("https://example.com/"), http.Response)
assert_type(http.fetch("https://example.com/", check=False), http.Response)
assert_type(http.fetch("https://example.com/", method="POST", body={"a": 1}).status, int)

http.fetch("https://example.com/", "POST")  # pyright: ignore[reportCallIssue]
http.fetch("https://example.com/", soft=True)  # pyright: ignore[reportCallIssue]
