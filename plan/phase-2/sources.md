# Sources

Phase 2. Changes flnd only. Do this before [commands](commands.md), which reads the registry.

Today the list of sources is written out four times: `SUPPORTED_SOURCES` and the `SourceCode` literal in [`flnd/target.py`](../../flnd/target.py), `SOURCES` in [`flnd/sources/__init__.py`](../../flnd/sources/__init__.py), and several docstrings. The new code would add three more copies: the help epilog, the source question in prompt mode, and the cookie domain table. Instead, each source class describes itself, and one registry lists the classes. Everything else is read from the registry.

```python
# flnd/sources/ins.py
class InsSource(BaseSource):
    code = "ins"                    # the prefix in a target, and the cookie file name
    name = "Instagram"              # shown in help and in prompt mode
    domains = ("instagram.com",)    # cookie domains that belong to this source

    def download(self, target, download_dir, limit=None, headless=True) -> int: ...
```

```python
# flnd/sources/__init__.py
# The one list of sources. Everything that names sources reads it.
SOURCES: dict[str, type[BaseSource]] = {
    source.code: source for source in (TikSource, FbkSource, InsSource, TwtSource)
}

def get_source(code: str) -> BaseSource: ...                  # ValueError naming the known codes
def source_for_domain(domain: str) -> str | None: ...         # cookie domain -> code, or None
```

`BaseSource` declares `code`, `name`, and `domains` as class attributes that every subclass must set.

| Place | Reads from the registry |
| --- | --- |
| `parse_target` in [`flnd/target.py`](../../flnd/target.py) | checks the code against `SOURCES`, so `ytb:nasa` still raises `ValueError` as [`tests/test_target.py`](../../tests/test_target.py) expects. `SUPPORTED_SOURCES` and the `SourceCode` literal are deleted, and `Target.source` is a `str`. `SOURCES` is imported inside the function, with a comment explaining why: `base.py` imports `Target`, so a top-level import would be circular |
| `get_source(code)` | an unknown code raises `ValueError("unknown source 'abc'. Sources: tik, fbk, ins, twt")`; `parse_target` uses the same message |
| the app's `description` and the epilog's `Sources:` line | built from `name` and `code` |
| `ask_targets` | the choices are `{source.name: code}` |
| `store_cookies` | `source_for_domain` sorts each cookie |
| `ask_for_cookies` | shows `name` ("No cookies for Instagram…") |

`source_for_domain(domain)` lowercases the domain and strips a leading `.`. It then matches a source whose domain is equal to it or a parent of it, so `www.instagram.com` and `.instagram.com` both give `ins`, while `notinstagram.com` gives nothing. No match returns `None`.

**Adding a source takes two steps:**

1. Write `flnd/sources/<code>.py` with a `BaseSource` subclass that sets `code`, `name`, and `domains` and implements `download`.
2. Add the class to the tuple in `SOURCES`.

Target parsing, help, prompt mode, and cookie detection all pick it up. The registry test (see [Tests](#tests)) catches the usual mistakes: a missing attribute, a duplicate code, or two sources claiming one domain.

## Tests

- every class in `SOURCES` sets `code`, `name`, and a non-empty `domains`;
- codes are unique;
- no domain belongs to two sources;
- `source_for_domain` handles a leading `.`, subdomains, mixed case, and a look-alike such as `notinstagram.com`;
- `get_source` and `parse_target` reject an unknown code with the same message;
- a fake extra source added to `SOURCES` in a test shows up in `parse_target`, `get_source`, the epilog, `ask_targets`, and `store_cookies`.
