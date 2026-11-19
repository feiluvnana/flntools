# Errors

What can go wrong in a run, which error class says so, what flnd does about it, and what you
can do.

## The classes

Every error flnd raises is a `flnd.Error`, from one hierarchy
([Where the classes live](#where-the-classes-live)):

```
flnd.Error                 str(e) says what went wrong; e.hint what to do
├── UsageError             a bad target, flag or config value
├── LoginError             not logged in, expired, or refused
├── SiteError              the site answered, but not usefully (e.status, e.response)
│   ├── Blocked            rate limited or blocked for a while (e.wait)
│   ├── NotFound           deleted, or never there
│   ├── Hidden             it exists, but this account can't see it (a private profile)
│   └── Changed            the answer no longer has the shape the plugin expects
├── NetworkError           DNS, TLS, a timeout, a reset, a transfer cut short
├── DiskError              no space, no permission, a name too long, a damaged store
├── ToolError              ffmpeg, 7zz or a browser: missing, or failed
└── PluginError            a plugin is broken: its declaration, or a bug in its listing

flnd.Stopped               Ctrl-C: a KeyboardInterrupt, never an Error
```

Each error has:

- **its message**, `str(error)`: one line, with no secret in it;
- **`hint`**: what to do, as a command when there is one (`run flnd auth login <code>`); `""` when
  there is nothing to suggest;
- **`target`**: the target it is about, as written (`ins:example_user`), or `None`;
- **`retryable`**: whether trying again later may work. Whoever raises the error sets it, from
  the policy below; the class alone doesn't decide it. A `NetworkError` for a host that doesn't
  exist won't heal, while one for a timeout may.

A `SiteError` also has `status`, the HTTP status (`None` when there was no HTTP answer), and
`response`, the answer itself, so a plugin can read the site's reason from its body.
`Blocked` has `wait`, the seconds the site asked to wait, or `None`.

`flnd.Stopped` derives from `KeyboardInterrupt`, so `except flnd.Error` (or `except Exception`)
never swallows a Ctrl-C.

```python
import flnd

try:
    with flnd.open() as fl:
        fl.get("zzz:example_user")
except flnd.Error as error:
    assert isinstance(error, flnd.UsageError)
    print(error)        # unknown source 'zzz'. Sources: tik, fbk, ins, twt, loi, tlg, mht

assert not issubclass(flnd.Stopped, flnd.Error)
assert issubclass(flnd.Blocked, flnd.SiteError)
```

## Where the classes live

The hierarchy is defined in two modules, because the kit, `flnkit`, imports nothing of flnd:

- **`flnkit/errors.py`** defines what the kit raises: `Error`, `UsageError`, `LoginError`,
  `SiteError`, `Blocked`, `NotFound`, `Changed`, `NetworkError`, `DiskError`, `ToolError` and
  `Stopped`, and `disk`, which turns an `OSError` into a `DiskError` with the `OSError` as its
  cause. Their hints are generic: none names a flnd command or flag.
- **`src/errors.py`** (`flnd.errors`) re-exports those same objects, so
  `flnd.LoginError is flnkit.errors.LoginError` and `except flnd.Error` catches what the kit
  raises. It adds what only flnd knows: `Hidden`, `PluginError` (plugin API 1 called it
  `SourceError`), and `classify`.

Each class's `__module__` is where it is defined: `flnkit.errors.LoginError`, but
`flnd.errors.PluginError`. The exit code goes by class (`isinstance`), never by that name, so a
plugin's own class that is merely named `LoginError` exits 4, not 3.

`classify` gives an error that has no hint flnd's own, before it is printed or kept in
`flnd.db`:

| error | flnd's hint |
| --- | --- |
| a `LoginError` about a target (`ins:example_user`) | `run flnd auth login ins` |
| a `DiskError` for a full disk or quota | `free some space on that disk, or use another --home` |
| a `DiskError` for a read-only disk | `that disk is read-only; use another --home` |
| a `DiskError` for a name too long | `use a shorter --home or folder template` |
| any other exception a plugin lets escape (a `PluginError`) | `this is a bug; run with -v and report it` |

A `ValueError` from the kit is its caller's mistake: `dig` raises one for a malformed path, and
a page's `css` for a selector that isn't CSS. Escaping a plugin, it becomes that plugin's
`PluginError`, never `Changed`.

A plugin imports the classes it raises from `flnd.plugin`: `UsageError`, `LoginError`,
`SiteError`, `Blocked`, `NotFound`, `Changed`, `NetworkError`, `PluginError`, and `Error` to
catch them all. A mistake in a plugin's declaration (a pattern outside its domains, a category
declared twice) raises `PluginError` when its class is made.

## The policy

| failure | class | retried | then |
| --- | --- | --- | --- |
| a timeout, a reset, a 5xx | `NetworkError`, or `SiteError` with the status | 4 more tries by the run's scheduler, waiting about 1, 2, 4 and 8 s (each times 0.5 to 1.5); a POST never, since the server may have acted | the file fails alone; the run goes on |
| a 429 | `Blocked` | its bucket (the host's, for the plugin) is held for what the site asks (`Retry-After`, or its rate-limit reset, up to 1 h), or 30 s when it doesn't say, and its pace halved for 20 requests; the third 429 in a row is a block round (below) | the file or target fails |
| a block the plugin's `blocked` hook reads from a page's content | `Blocked` | a block round of the run's scheduler: the plugin's account (every target of it) is held for the larger of what the site asks (up to 1 h) and the round's wait, 30 s, then 2 min, then 8 min for the rounds in a row; 10 minutes without a block start the rounds again; the fourth round in a row raises | the target fails |
| a 404 or 410 | `NotFound` | never | the file or target fails |
| an item this account can't see (the plugin raises `Hidden`) | `Hidden` | with switching on, on another account | the file or target fails; from a lazy post's files, that post alone |
| a 401 or 403 from the plugin's own domains, a login wall | `LoginError`, raised by `ctx.fetch` itself (the refusal is its cause) unless the plugin asked `check=False` | never | the plugin stops: its other targets fail with the same error, and the other plugins go on |
| a 401 or 403 from another host (a CDN's expired link) | `SiteError` | a 403 or 410 on a post whose files the plugin lists lazily lists them again once, for a fresh link | the file fails alone |
| a truncated transfer | `NetworkError` | yes, resuming from the part | the part is kept for the next run |
| an answer that doesn't parse, a web page where media was expected | `Changed`, `SiteError` | never | the file or target fails |
| no space, no permission, a name too long | `DiskError` | never | the run stops |
| a missing or failing ffmpeg, 7zz or browser | `ToolError` | a DASH merge that ffmpeg failed is tried 3 times in all; a missing tool never | the file or target fails |
| an unexpected exception in a listing | `PluginError` | never | the target fails, and its message says it is a bug |

What "fails" means at each level:

- **A file** that fails (after its tries) is counted as failed, written to `flnd.db`'s
  `failures`, and retried at the start of the target's next runs ([Retrying](#retrying)). A
  404 or 410 (`NotFound`) records the file as gone instead: it isn't asked for again unless told
  to (`--again`). The rest of the target goes on.
- **A target** whose listing fails ends with its error printed, then its hint on the next line,
  and `TargetReport.error` holds the error. The other targets still run.
- **A plugin** whose login fails, or that can't start (its `connect` fails), fails each of its
  targets; the other plugins still run.
- **A usage mistake** stops the command before anything is fetched.
- **A full or refused disk** stops the run; the files already saved keep their rows.

The run's exit code says which happened; [cli.md](cli.md#exit-codes) lists the codes: 1 for
failed files or targets, 2 for usage, 3 for a login, 4 when flnd itself stopped, 130 for Ctrl-C.

## No tracebacks for a person's mistake

A `flnd.Error` that ends a command, most often a `UsageError`, `LoginError`, `DiskError` or
`ToolError`, prints one line saying what went wrong and one line saying what to do, and never a
traceback. Its class gives the exit code: 2 for a `UsageError`, 3 for a `LoginError`, 4 for a
`DiskError`, `ToolError` or `PluginError`, and 1 for any other `flnd.Error`:

```
✗ ins:example_user needs a login
  hint  flnd auth login ins
```

An unexpected exception is a bug. The CLI prints `✗ flnd hit a bug: <the error>`, then
`hint  details in .flnd/log/crash-<time>.txt`, the crash file that holds the traceback, and
exits 4; with `-v` the traceback prints instead. A bug inside one target's listing doesn't
stop the run: it becomes that target's `PluginError`, and its cause is printed with `-v`.

## Secrets never reach a message

- A URL in a message is redacted: its user info, query and fragment are dropped.
- The secret headers a request carried (`Authorization`, `Cookie`, `Proxy-Authorization`), their
  parts (a cookie's value, a bearer token) and the URL's query values are replaced by `***`
  wherever a library's error text quotes them.
- A cookie's value is never printed, logged or put in an error, even when a cookie file is
  broken: the message names the file and the line.

## The failure report

Every failure is a row of `flnd.db`'s `failures` table ([storage.md](storage.md)):
its target, post, category, the file it was to be, the error's class, message and hint, how
many runs failed it (`attempts`), and its URL, redacted. That row is the only record, so the
end of a run and `flnd status` can't disagree. A file has one row however often
it fails, and a file saved since drops out of the report.

The end of a run sums its failures up in one line on standard error, by kind, saying which a
retry may bring back:

```
✗ 3 files failed: 2 network (retry: flnd get), 1 gone. Details: flnd status
```

The kinds are `network`, `blocked`, `site`, `changed`, `login`, `disk`, `tool`, `bug` (a
`PluginError`), `usage` and `gone` (a `NotFound`, which no retry asks for again).

`flnd status` shows the last run that downloaded: what it saved, and each failure it left,
with its message and hint. `flnd status --json` prints the same as one JSON object for other
tools, and `fl.status` returns it as a `flnd.Status` ([library.md](library.md#reading-the-home)).

### Samples of changed answers

An answer that doesn't have the shape a plugin expects (`Changed`, with the answer in
`e.response`) is kept as a sample, so a bug report can show what the site sent:
`log/<run>/samples/<n>.txt`, its first line the redacted URL. The failure's hint names the
file. A sample is scrubbed before it is written:

- every value under a key that looks like a secret (`token`, `csrf`, `session`, `cookie`,
  `auth`, `password`, `secret`, `key`, `sig`, anywhere in the name and in any case) becomes `"…"`,
  in JSON at any depth, and in HTML forms and `name=value` text;
- a login's answer is never kept: one whose URL looks like a login (`login`, `auth`, `session`,
  `token`, `account`, …), or that sets a cookie;
- the file is named by a count, never by anything the site sent.

Each run's logs and samples are swept after 30 days, at the start of a later run.

## Retrying

A retry never reuses a stored URL: the one `flnd.db` keeps is redacted, and a CDN's signed URL
expires within hours anyway. It works from `(target, post, file)` instead: it lists the failed
post again through its plugin, starting from the cursor of the page the post was on, so a post
deep in a long listing costs one page, and downloads the post's files that aren't saved, which
are the ones that failed. A post the listing no longer has is recorded gone.

- **By itself.** Every `get` of a target first retries that target's failed
  files, before its new posts. A file is retried this way on at most 3 runs (its `attempts`
  reach 4: the first failure and three retries); after that it waits for `r` in `flnd target list`
  (`fl.targets.retry()`). A gone file (`NotFound`) is never retried.
- **`r` in `flnd target list`** (`fl.targets.retry(target)`) makes a target's failed files
  retryable again, however many runs failed them: the next `flnd get` lists the failed posts'
  pages and nothing else for them. A target whose whole listing failed is downloaded again.

A file a retry brought back is a `Saved` event with `retried=True`; the end of the run says
`N brought back by a retry`.

## Not built yet

- One cookie refresh from the browser before a 401 or 403 becomes a `LoginError`.

## Common messages

| message | meaning | fix |
| --- | --- | --- |
| `<target> is one post, with no categories` | a single post has no categories | leave the `/categories` out |
| `<target>: not logged in` | the plugin requires a login | `flnd auth login <code>` |
| `<code>: not logged in, using public access` | a warning: only public posts are listed | log in to get more |
| `<code>: the site refused the login (HTTP 403)` | the saved login expired or was revoked | `flnd auth login <code>` |
| `which source is "<text>"?` | a bare name, not a link, without a plugin's code | write it as `<code>:<text>` |
| `got a web page instead of a photo` | the site answered a file request with a page | usually a login wall: log in again |
| `<home>/flnd.db: database is locked` | another flnd run held the home's database for more than 30 s | wait for it to end, or use another `--home` |
| `unexpected <Type>: …` | a bug in a plugin or in flnd | run with `-v` and report it |

## Module `flnd.errors`

flnd's face of the one error hierarchy: flnkit's classes, `Hidden`, `PluginError`, `classify`.

src/docs/errors explains it. The classes flnkit raises are defined in `flnkit.errors` and are
the same objects here (`flnd.LoginError is flnkit.errors.LoginError`); `Hidden`, `PluginError`
and the hints that name flnd's commands and flags are flnd's own.

## Module `flnkit.net`

HTTP for any program on an asyncio loop: a `Session`, a `Client`, and downloads into parts.

What it offers, for any caller:
- `Session`: libcurl handles on one multi handle, through curl_cffi, kept from one transfer to
  the next (Chrome's TLS and HTTP/2 fingerprint), its default headers, its cookies, and a
  `prepare` hook that may add headers to each request before it goes out (a signature, a CSRF
  token).
- `Client`: a session with a stop event. `fetch` sends one request; `fill` streams one try of a
  file into a `flnkit.files.Part`, going on from the bytes it holds. Neither paces nor retries:
  the caller's scheduler does (flnd's is `flnd.engine.jobs`).
- `follow(url)`: where a short link leads, from one HEAD request (blocking, for any thread).
- `fake(routes)`: answers every request in the block from routes or a handler, never the
  network; `reply(...)` builds an answer for a handler.

    session = Session(headers={"User-Agent": agent}, cookies=jar, prepare=sign)
    client = Client(session, stop=event)
    page = await client.fetch("https://example.com/api", body={"q": "x"})   # page.json
    with files.part(dest, resume=True) as held:
        headers = await client.fill(url, held, progress=row)
    await session.close()

What a failure raises (src/docs/errors):
- a timeout, a reset or a cut-off transfer is a retryable `NetworkError`, a 5xx a retryable
  `SiteError`;
- a 429 is `Blocked`, with its `Retry-After`, or the site's rate-limit reset, as `wait` (at most
  an hour);
- a 404 or 410 is `NotFound`, any other 4xx a `SiteError`; neither is retryable;
- a malformed URL, a name that doesn't resolve or a certificate that fails is a `NetworkError`
  that isn't retryable; a write the disk refuses is a `DiskError`; a set `stop` is `Stopped`.
A refusal carries its `Response` as `error.response`.

Every message names the URL redacted (`flnkit.text.redact`), and any secret the request carried
(a cookie, a token, a query value) is scrubbed from a library's error text.

Rules a change must keep:
- A session belongs to one asyncio loop: every coroutine here runs on it, and nothing here
  blocks it. File writes go to a writer thread, `BUFFER` bytes at a time: the caller's (`Meter`'s
  `write`), else this module's one. So a slow disk never stalls the loop (and the browser and
  every other transfer on it); see src/docs/performance.
- A write in flight on those threads always finishes before its transfer gives the part
  back, even when the transfer is cancelled: the part's descriptor is closed only after it.
