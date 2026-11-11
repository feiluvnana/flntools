# flnd commands

Phase 2. Needs [cli](../phase-1/cli.md), [prompt](../phase-1/prompt.md), and [sources](sources.md).

## Command line

| Today | After |
| --- | --- |
| `flnd [targets] -o -l --headless` | `flnd download targets...` with no options |
| no cookie command | `flnd auth set-cookie --path PATH`, `--content TEXT`, or the cookie text piped in |
| `create_parser` (about 50 lines) | signatures and docstrings |
| `flnd = "flnd.cli:main"` in [`pyproject.toml`](../../pyproject.toml) | `flnd = "flnd.cli:app"` |
| [`flnd/__main__.py`](../../flnd/__main__.py) calls `main()` | it calls `app()` |
| `flnd` alone prints help | on a terminal it starts prompt mode; otherwise it prints help |
| targets only from argv | targets from argv, or one per line from stdin |

`download` drops `--output`, `--limit`, and `--headless`. Output is `.flnd/downloads`, there is no limit, and the browser is visible. The `--limit` example in [`README.md`](../../README.md) goes away, and the README's commands change to `flnd download …`. The README also gains `cat list.txt | flnd download` and a short section on prompt mode.

`targets` is `"+"`. `flnd download` with no targets and no piped input is a usage error.

```python
names = [source.name for source in SOURCES.values()]
codes = ", ".join(SOURCES)

app = cli.App(
    "flnd",
    version="0.1.0",
    description=f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}.",
    epilog=f"""
Target format:
  <source>:<want to download>
  Sources: {codes}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle
  cat list.txt | flnd download
  flnd auth set-cookie --path ~/Downloads/cookies.txt
""",
    prompt_mode=True,
)

@app.command
def download(targets: Annotated[list[str], cli.arg(nargs="+", ask=ask_targets)]) -> int:
    """Download each target.

    Args:
        targets: source:value pairs.
    """
    try:
        parsed = [parse_target(raw) for raw in targets]
    except ValueError as err:
        log.error("{err}", err=err)
        return 1

    output = Path(".flnd/downloads").resolve()
    log.info("Storage: {path}", path=output)
    total = 0
    with log.multi("download") as jobs:
        rows = [(target, jobs.add(str(target))) for target in parsed]
        for target, row in rows:
            try:
                total += get_source(target.source).download(
                    target=target, download_dir=output, limit=None, headless=False
                )
                row.finish()
            except Exception as err:
                log.error("{target}: {err}", target=target, err=err)
                row.fail()
    log.ok("Complete. {n} files", n=total)
    return 0

auth = app.group("auth", description="Cookie files for a source.")

@auth.command("set-cookie")
def set_cookie(
    path: Annotated[Path | None, cli.opt(ask=ask_cookie_path)] = None,
    content: Annotated[str | None, cli.opt(stdin=True)] = None,
) -> int:
    """Set a cookie file.

    Each cookie is stored under the source its domain belongs to.

    Args:
        path: Netscape cookies.txt or .json file.
        content: the cookie text itself; read from stdin when piped.
    """
    if path and content:
        raise cli.UsageError("give --path or --content, not both")
    if not path and not content:
        raise cli.UsageError("give --path, --content, or pipe the cookie text in")
    text = path.read_text(encoding="utf-8") if path else content
    try:
        saved = store_cookies(text)
    except ValueError as err:
        log.error("{err}", err=err)
        return 1
    for source, (count, stored) in saved.items():
        log.ok("{source}: {n} cookies saved to {path}", source=source, n=count, path=stored)
    return 0
```

### set-cookie

`set-cookie` takes the cookie text in one of three ways. Exactly one must be given:

```sh
flnd auth set-cookie --path ~/Downloads/cookies.txt   # a file
flnd auth set-cookie --content "$(pbpaste)"           # the raw text
pbpaste | flnd auth set-cookie                        # the raw text from stdin
```

The text is not tied to a source by the user. `store_cookies(text)` in [`flnd/cookies.py`](../../flnd/cookies.py) works it out:

1. **Detect the format.** Text whose first non-blank character is `[` or `{` is JSON. Anything else is Netscape `cookies.txt`.
2. **Parse it.** `parse_netscape_cookies` and `parse_json_cookies` are split into text parsers (`parse_netscape_text`, `parse_json_text`) and the existing file readers, which call them. Text that parses to no cookies raises `ValueError("no cookies found")`.
3. **Detect the source.** Each cookie goes to `source_for_domain(cookie["domain"])` (see [sources.md](sources.md)). Today the domains are `tiktok.com` → `tik`, `facebook.com` → `fbk`, `instagram.com` → `ins`, and `x.com` / `twitter.com` → `twt`, but that list lives in the source classes, not here.
   - Cookies for other sites are left out. A full browser export carries many of these, so leaving them out is not an error. A `log.info("skipped {n} cookies for other sites")` line says how many.
   - If no cookie matches any source, nothing is written and it raises `ValueError("no cookies for any source (tik, fbk, ins, twt)")`. The code list is read from `SOURCES`. `set-cookie` turns that into `✗` and exit `1`.
4. **Write.** Each source's cookies are written to `.flnd/cookies/<source>.txt` in Netscape format, whatever format the input was. This is the file name flnd already uses and the format browser extensions usually export, and it is one cookie per line, so it is easy to read and edit. `write_netscape(cookies)` writes the seven tab-separated fields per line: domain, `TRUE` when the domain starts with `.`, path, secure, expiry, name, value. A JSON cookie's `expirationDate` becomes the expiry (`0` when missing). `httpOnly` is not kept, because today's parser ignores it too. Any old `.flnd/cookies/<source>.json` is deleted so that only one file remains for the source.

A browser export that holds cookies for several sites sets every matching source in one run:

```text
$ flnd auth set-cookie --path ~/Downloads/cookies.txt
✓ ins: 14 cookies saved to .flnd/cookies/ins.txt
✓ twt: 9 cookies saved to .flnd/cookies/twt.txt
```

Targets are parsed before the block opens, so a bad target exits `1` without drawing any rows. A failed target ends its row at `error`, and the run goes on to the next target.

## Prompt mode

`flnd` alone, on a terminal:

```text
What do you want to do?
› ● Download each target
  ○ Cookie files for a source
```

`Download each target` asks for targets with `ask_targets` rather than one plain text input. It asks for one target at a time, so the user never needs to know the `source:value` format:

```python
def ask_targets() -> list[str]:
    sources = {source.name: code for code, source in SOURCES.items()}
    targets = []
    while True:
        source = prompt.choice("Source", sources)
        value = prompt.input("Username or id", validate=nonempty)
        targets.append(f"{source}:{value}")
        if not prompt.confirm("Add another target?", default=False):
            return targets
```

```text
✓ What do you want to do? Download each target
✓ Source: Instagram
✓ Username or id: example_user
✓ Add another target? No
• flnd download ins:example_user
• Storage: /…/.flnd/downloads
• download
• ins:example_user ⠙
```

From that point on it is the same run as cli mode.

`Cookie files for a source` opens the `auth` group's list. That list has one row, `Set a cookie file`, and picking it calls `ask_cookie_path`:

```python
def ask_cookie_path() -> Path:
    return Path(prompt.input("Path to cookies.txt or .json", validate=readable_cookie_file)).expanduser()
```

`readable_cookie_file` checks that the file exists and that `store_cookies` would find cookies in it. `content` is optional and has no `ask=`, so prompt mode does not ask for it. Pasting a multi-line cookie file into a one-line input would not work anyway. The logged command line is `flnd auth set-cookie --path ~/Downloads/cookies.txt`.

## Missing cookies

`prompt_missing_cookies(source)` becomes `ask_for_cookies(source) -> Path | None`.

On an interactive screen:

1. `prompt.confirm("No cookies for {name}. Set a cookie file now?", default=True)`, where `name` is the source's `name`.
2. If yes, `ask_cookie_path()`, the same question prompt mode asks.
3. `store_cookies` stores it, the same as `set-cookie`. The function returns `.flnd/cookies/<source>.txt` when that source was among the sources saved. If the file had no cookies for that source, it writes `log.warn` and returns `None`.

When the screen is not interactive, it writes `log.error` for the missing file and `log.info` for where to put it, then returns `None`. That is the same information the four prints give today.

Because the prompt pauses the live area, it can be asked in the middle of the `download` block.

## Tests

- `app.run` covers each flnd command line above, including targets from stdin.
- Prompt mode, driven by `read_key`, reaches `download` through `ask_targets` (one target and two targets) and reaches `set-cookie`.
- `ask_for_cookies` covers both an interactive and a non-interactive screen.
- `set-cookie`:
  - each of the three inputs, `--path` together with `--content`, and no input at all;
  - Netscape and JSON input, and a file with several sources;
  - a file with no matching domain, which raises, writes nothing, and exits `1`;
  - a file mixing known and unknown domains, which skips the unknown ones and logs a count;
  - the written `.txt` read back by `parse_netscape_cookies`;
  - an old `<source>.json` being removed.
