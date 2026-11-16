# Errors

What can go wrong in a run, which error class says so, what flnd does about it, and what you
can do.

## The classes

Every error flnd raises is a `flnd.Error`, from one hierarchy in `flnd/errors.py`:

```
flnd.Error                 str(e) says what went wrong; e.hint what to do
├── UsageError             a bad target, flag or config value
├── LoginError             not logged in, expired, or refused
├── SiteError              the site answered, but not usefully (e.status, e.response)
│   ├── Blocked            rate limited or blocked for a while (e.wait)
│   ├── NotFound           deleted, private, or never there
│   └── Changed            the answer no longer has the shape the source expects
├── NetworkError           DNS, TLS, a timeout, a reset, a transfer cut short
├── DiskError              no space, no permission, a name too long, a damaged store
├── ToolError              ffmpeg, 7zz or a browser: missing, or failed
└── SourceError            a source is broken: its declaration, or a bug in its listing

flnd.Stopped               Ctrl-C: a KeyboardInterrupt, never an Error
```

Each error has:

- **its message**, `str(error)`: one line, with no secret in it;
- **`hint`**: what to do, as a command when there is one (`run flnd login <code>`); `""` when
  there is nothing to suggest;
- **`target`**: the target it is about, as written (`ins:example_user`), or `None`;
- **`retryable`**: whether trying again later may work. Whoever raises the error sets it, from
  the policy below; the class alone doesn't decide it. A `NetworkError` for a host that doesn't
  exist won't heal, while one for a timeout may.

A `SiteError` also has `status`, the HTTP status (`None` when there was no HTTP answer), and
`response`, the answer itself, so a source can read the site's reason from its body.
`Blocked` has `wait`, the seconds the site asked to wait, or `None`.

`flnd.Stopped` derives from `KeyboardInterrupt`, so `except flnd.Error` (or `except Exception`)
never swallows a Ctrl-C.

```python
import flnd

try:
    with flnd.open() as fl:
        fl.download("zzz:example_user")
except flnd.Error as error:
    assert isinstance(error, flnd.UsageError)
    print(error)        # unknown source 'zzz'. Sources: tik, fbk, ins, twt, loi, tlg, mht

assert not issubclass(flnd.Stopped, flnd.Error)
assert issubclass(flnd.Blocked, flnd.SiteError)
```

A source imports the classes it raises from `flnd.source`: `UsageError`, `LoginError`,
`SiteError`, `Blocked`, `NotFound`, `Changed`, `NetworkError`, `SourceError`, and `Error` to
catch them all. A mistake in a source's declaration (a pattern outside its domains, a category
declared twice) raises `SourceError` when its class is made.

## The policy

| failure | class | retried | then |
| --- | --- | --- | --- |
| a timeout, a reset, a 5xx | `NetworkError`, or `SiteError` with the status | 4 more tries, waiting about 1, 2, 4 and 8 s (jittered); a POST never, since the server may have acted | the file fails alone; the run goes on |
| a 429 or a block page | `Blocked` | waits what the site asks (`Retry-After`, or its rate-limit reset, up to 1 h), or 1 then 2 s when it doesn't say; on the source's own domains its pace is held and halved for 20 requests; the third 429 in a row raises | the file or target fails |
| a block a source reads from a page's content (`site.back_off`) | `Blocked` | waits what the site asks (up to 1 h), or 30 s, then 2 min, then 8 min for the rounds in a row; 10 minutes without a block start the rounds again; the fourth round in a row raises | the target fails |
| a 404 or 410, a private item | `NotFound` | never | the file or target fails |
| a 401 or 403 from the source's own domains, a login wall | `LoginError`, raised by `site.fetch` itself (the refusal is its cause) unless the source asked `check=False` | never | the source stops: its other targets fail with the same error, and the other sources go on |
| a 401 or 403 from another host (a CDN's expired link) | `SiteError` | a 403 or 410 on a post whose files the source lists lazily lists them again once, for a fresh link | the file fails alone |
| a truncated transfer | `NetworkError` | yes, resuming from the part | the part is kept for the next run |
| an answer that doesn't parse, a web page where media was expected | `Changed`, `SiteError` | never | the file or target fails |
| no space, no permission, a name too long | `DiskError` | never | the run stops |
| a missing or failing ffmpeg, 7zz or browser | `ToolError` | a DASH merge that ffmpeg failed is tried 3 times in all; a missing tool never | the file or target fails |
| an unexpected exception in a listing | `SourceError` | never | the target fails, and its message says it is a bug |

What "fails" means at each level:

- **A file** that fails (after its tries) is counted as failed, written to `flnd.db`'s
  `failures`, and retried at the start of the target's next runs ([Retrying](#retrying)). A
  404 or 410 (`NotFound`) records the file as gone instead: it isn't asked for again unless told
  to (`--again`). The rest of the target goes on.
- **A target** whose listing fails ends with its error printed, then its hint on the next line,
  and `TargetReport.error` holds the error. The other targets still run.
- **A source** whose login fails, or that can't start (its `connect` fails), fails each of its
  targets; the other sources still run.
- **A usage mistake** stops the command before anything is fetched.
- **A full or refused disk** stops the run; the files already saved keep their rows.

The run's exit code says which happened; [cli.md](cli.md#exit-codes) lists the codes: 1 for
failed files or targets, 2 for usage, 3 for a login, 4 when flnd itself stopped, 130 for Ctrl-C.

## No tracebacks for a person's mistake

A `flnd.Error` that ends a command, most often a `UsageError`, `LoginError`, `DiskError` or
`ToolError`, prints one line saying what went wrong and one line saying what to do, and never a
traceback. Its class gives the exit code: 2 for a `UsageError`, 3 for a `LoginError`, 1 for a
`SiteError` or `NetworkError`, and 4 for a `DiskError`, `ToolError` or `SourceError`:

```
x ins:example_user: not logged in
• ins:example_user: run flnd login ins
```

An unexpected exception is a bug. The CLI prints one line naming a crash file,
`.flnd/logs/crash-<time>.txt`, which holds the traceback, and exits 4; with `-v` the traceback
prints instead. A bug inside one target's listing doesn't stop the run: it becomes that target's
`SourceError`, and its cause is printed with `-v`.

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
end of a run, `flnd status` and `flnd retry` can't disagree. A file has one row however often
it fails, and a file saved since drops out of the report.

The end of a run sums its failures up in one line on standard error, by kind, saying which a
retry may bring back:

```
✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status
```

The kinds are `network`, `blocked`, `site`, `changed`, `login`, `disk`, `tool`, `bug` (a
`SourceError`), `usage` and `gone` (a `NotFound`, which no retry asks for again).

`flnd status` shows the last run that downloaded: what it saved, and each failure it left,
with its message and hint. `flnd status --json` prints the same as one JSON object for other
tools, and `fl.status` returns it as a `flnd.Status` ([library.md](library.md#reading-the-home)).

### Samples of changed answers

An answer that doesn't have the shape a source expects (`Changed`, with the answer in
`e.response`) is kept as a sample, so a bug report can show what the site sent:
`logs/<run>/samples/<n>.txt`, its first line the redacted URL. The failure's hint names the
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
post again through its source, starting from the cursor of the page the post was on, so a post
deep in a long listing costs one page, and downloads the post's files that aren't saved, which
are the ones that failed. A post the listing no longer has is recorded gone.

- **By itself.** Every `download` and `sync` of a target first retries that target's failed
  files, before its new posts. A file is retried this way on at most 3 runs (its `attempts`
  reach 4: the first failure and three retries); after that it waits for `flnd retry`. A gone
  file (`NotFound`) is never retried.
- **`flnd retry`** (`fl.retry()`) retries every failed file of every target at once, however
  many runs failed it, and lists nothing but the failed posts' pages. A target whose whole
  listing failed is downloaded again.

A file a retry brought back is a `Saved` event with `retried=True`; the end of the run says
`N brought back by a retry`.

## Not built yet

- One cookie refresh from the browser before a 401 or 403 becomes a `LoginError`.

## Common messages

| message | meaning | fix |
| --- | --- | --- |
| `--only applies to profiles and chats; <target> is one post` | a single post has no categories | leave `--only` out |
| `<target>: not logged in` | the source requires a login | `flnd login <code>` |
| `<code>: not logged in, using public access` | a warning: only public posts are listed | log in to get more |
| `<code>: the site refused the login (HTTP 403)` | the saved login expired or was revoked | `flnd login <code>` |
| `which source is "<text>"?` | a bare name, not a link, without a source | write it as `<code>:<text>` |
| `got a web page instead of a photo` | the site answered a file request with a page | usually a login wall: log in again |
| `<home>/flnd.db: database is locked` | another flnd run held the home's database for more than 30 s | wait for it to end, or use another `--home` |
| `unexpected <Type>: …` | a bug in a source or in flnd | run with `-v` and report it |
