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
│   └── BusyError          another run is saving this file
├── ToolError              ffmpeg, 7zz or a browser: missing, or failed
│   └── RunError           a tool ran and failed (its exit code, or a timeout)
└── PluginError            a plugin is broken: its declaration, or a bug in its listing

flnd.Stopped               Ctrl-C: a KeyboardInterrupt, never an Error
```

Each error has:

- **its message**, `str(error)`: one line, with no secret in it;
- **`hint`**: one line on what to do, as a command when there is one (`flnd auth login ins`);
  `""` when there is nothing to suggest;
- **`target`**: the target it is about, as written (`ins:example_user`), or `None`;
- **`retryable`**: whether trying again later may work. The code that raises the error sets it;
  the class alone doesn't decide it. A `NetworkError` for a host that doesn't exist won't heal,
  while one for a timeout may.

A `SiteError` also has `status`, the HTTP status (`None` when there was no HTTP answer), and
`response`, the answer itself, so a plugin can read the site's reason from its body. `Blocked`
has `wait`, the seconds the site asked to wait, or `None`. A `DiskError` has `path`, and a
`ToolError` has `command` and `tail`, the last lines of the tool's output.

`flnd.Stopped` derives from `KeyboardInterrupt`, so `except flnd.Error` (or `except Exception`)
never swallows a Ctrl-C.

```python
from flnd import errors

error = errors.LoginError("ins: not logged in", target="ins:example_user")
assert errors.classify(error).hint == "run flnd auth login ins"

assert issubclass(errors.Blocked, errors.SiteError)
assert not issubclass(errors.Stopped, errors.Error)
```

## Where the classes live

The hierarchy is defined in two modules, because the kit, `flnkit`, imports nothing of flnd:

- **`flnkit/errors.py`** defines what the kit raises: `Error`, `UsageError`, `LoginError`,
  `SiteError`, `Blocked`, `NotFound`, `Changed`, `NetworkError`, `DiskError`, `ToolError` and
  `Stopped`. `disk` turns an `OSError` into a `DiskError`, with the `OSError` as its cause. The
  kit's hints are generic: none names a flnd command or flag.
- **`src/errors.py`** (`flnd.errors`) re-exports those same objects, so
  `flnd.LoginError is flnkit.errors.LoginError`, and `except flnd.Error` catches what the kit
  raises. It adds what only flnd knows: `Hidden`, `PluginError`, `classify`, and flnd's hints
  (`DISK_HINTS`, `LOGIN_HINT`, `BUG_HINT`).

`flnkit/net.py` raises the HTTP errors: its `refusal` turns a response into its class, and a
failed transfer becomes a `NetworkError`.

Each class's `__module__` is where it is defined: `flnkit.errors.LoginError`, but
`flnd.errors.PluginError`. The exit code goes by class (`isinstance`), never by name, so a
plugin's own class that is merely named `LoginError` exits 4, not 3.

`classify` gives an error that has no hint of its own flnd's hint, before it is printed or kept
in `flnd.db`. A permission error keeps the kit's hint, `check that you may write to that folder`.

- A `LoginError` about a target (`ins:example_user`): `run flnd auth login ins`.
- A `DiskError` for a full disk or a quota: `free some space on that disk, or use another --home`.
- A `DiskError` for a read-only disk: `that disk is read-only; use another --home`.
- A `DiskError` for a name that is too long: `use a shorter --home or folder template`.
- Any other exception a plugin lets escape, made a `PluginError`:
  `this is a bug; run with -v and report it`.

A `ValueError` from the kit is its caller's mistake: `dig` raises one for a malformed path, and a
page's `css` raises one for a selector that isn't CSS. Escaping a plugin, it becomes that
plugin's `PluginError`, never `Changed`. A connection or timeout error becomes a retryable
`NetworkError`.

A plugin imports the classes it raises from `flnd.plugin`: `UsageError`, `LoginError`,
`SiteError`, `Blocked`, `NotFound`, `Changed`, `NetworkError`, `PluginError`, and `Error` to
catch them all. A mistake in a plugin's declaration (a pattern outside its domains, a category
declared twice) raises `PluginError` when its class is made.

## The policy

The scheduler (`src/engine/jobs.py`) decides the retries. The code that raises an error decides
its class and whether it is `retryable`.

- **A timeout, a reset, a cut connection, or a 5xx.** It is a `NetworkError`, or a `SiteError`
  for a 5xx, and retryable. The scheduler tries up to 4 more times, waiting about 1, 2, 4 and 8 s
  (each times 0.5 to 1.5). Then the file fails alone, and the run goes on.
- **An unsupported protocol, a malformed URL, a host or proxy that doesn't resolve, too many
  redirects, or a certificate that fails.** It is a `NetworkError`, not retryable. The file
  fails at once.
- **A 429.** It is a `Blocked`, with `wait` from `Retry-After` or the site's rate-limit reset
  (at most 1 h), or 30 s when the site names no wait. The bucket (the host's, for the plugin)
  waits that long, and its pace halves until 20 requests have gone through. The third 429 in
  a row is a block round (below). Then the file or target fails.
- **A block that the plugin's `blocked` hook reads from a page.** It is a `Blocked`, and a block
  round: the plugin's account (every target of it) is held for the larger of the site's wait and
  the round's wait. The rounds wait 30 s, then 2 min, then 8 min. Ten minutes without a block
  start the rounds again. After the fourth round in a row, the target fails.
- **A 404 or 410.** It is a `NotFound`, never retried. The file is recorded as gone, and the
  target goes on. A target's first page is moved to another account when `switch` is on.
- **An item this account can't see.** It is a `Hidden`. With `switch` on, the job moves to
  another account. Otherwise the file or target fails. For a lazy post's files, that post alone
  fails.
- **A 401 or 403 from the plugin's own domains.** It is a `LoginError`, not retryable. With
  `switch` on, the job moves to another account. Otherwise the account is marked expired in
  `flnd.db`, and the target fails.
- **A login wall.** It is a retryable `LoginError`, raised when a page lands on a wall the plugin
  declares. The account is held 30 s, then 2 min. After that it is handled as a 401 or 403.
- **A 403 or 410 on a lazy post's file.** The link has expired. The post's files are listed
  again once, for a fresh link. If that fails too, the file fails alone.
- **A truncated transfer.** It is a retryable `NetworkError`. The transfer resumes from the part
  kept on disk, and the part stays for the next run if this try ends.
- **An answer that doesn't parse.** It is a `Changed`, never retried. The file or target fails,
  and a sample of the answer is kept ([Samples](#samples-of-changed-answers)).
- **A web page where media was expected.** It is a `SiteError`, never retried. The file fails,
  and its hint says it is usually a login wall.
- **No space, no permission, or a name too long.** It is a `DiskError`, never retried. The run
  stops.
- **A part of a file that another run is saving.** It is a `BusyError`, a `DiskError` that
  stops nothing. That file alone is skipped.
- **`flnd.db` locked by another process.** The job goes back in the queue every 20 ms, for up to
  30 s. Then the run stops, with the hint `let the other run finish`.
- **A missing ffmpeg, 7zz or browser.** It is a `ToolError`. The run stops.
- **A tool that ran and failed.** It is a `RunError`, a `ToolError`. A failed DASH join is
  retryable, so the scheduler tries it up to 4 more times, as for a network failure. Then the
  file fails.
- **An unexpected exception in a listing.** It is a `PluginError`, never retried. The target
  fails with the bug hint. A plugin class that is broken stops the run.

What "fails" means at each level:

- **A file** that fails (after its tries) is counted as failed, written to `flnd.db`'s
  `failures`, and retried at the start of its target's next runs ([Retrying](#retrying)). A 404
  or 410 (`NotFound`) records the file as gone instead: it isn't asked for again unless told to
  (`--again`). The rest of the target goes on.
- **A target** whose listing fails gets a failed event with no post. Its error is printed, then
  its hint on the next line. The other targets still run.
- **A target that needs a login it lacks** fails with `not logged in`. The other targets go on.
- **A usage mistake** stops the command before anything is fetched.
- **A full or refused disk** stops the run. The files already saved keep their rows.

The run's exit code says which happened. [cli.md](cli.md#exit-codes) lists the codes: 1 for
failed files or targets, 2 for usage, 3 for a login, 4 when flnd itself stopped, 130 for Ctrl-C.

## No tracebacks for a person's mistake

A `flnd.Error` that ends a command, most often a `UsageError`, `LoginError`, `DiskError` or
`ToolError`, prints one line saying what went wrong and one line saying what to do, and never a
traceback. Its class gives the exit code: 2 for a `UsageError`, 3 for a `LoginError`, 4 for a
`DiskError`, `ToolError` or `PluginError`, and 1 for any other `flnd.Error`:

```
✗ no plugin named zzz
  hint  Plugins: tik, fbk, ins, …
```

An unexpected exception is a bug. The CLI prints `✗ flnd hit a bug: KeyError: 'x'`, then
`  hint  details in .flnd/log/crash-<time>.txt`, the crash file that holds the traceback. The
file is in `<home>/log/`, and the command exits 4. With `-v` the traceback prints instead. So
does the traceback of any crash whose file can't be written. A bug inside one target's listing
doesn't stop the run: it becomes that target's `PluginError`, and its cause is printed with `-v`.

## Secrets never reach a message

- A URL in a message is redacted: its user info, query and fragment are dropped
  (`flnkit.text.redact`).
- The secret headers a request carried (`Authorization`, `Cookie`, `Proxy-Authorization`), their
  values, and the URL's query values are replaced by `***` wherever a library's error text
  quotes them (`_scrub` in `flnkit/net.py`).
- A value under a name that looks like a secret is replaced by `…` in a kept sample
  (`flnkit.text.scrub`).
- A cookie's value is never printed, logged or put in an error, even when a cookie file is
  broken. The message names the file and the line.

## The failure report

Every failure is a row of `flnd.db`'s `failures` table ([storage.md](storage.md)). The row holds
its target, post, category, the file it was to be, the error's class, message and hint, how
many runs failed it (`attempts`), whether it is retryable, and its URL, redacted. That row is the
only record, so the end of a run and `flnd status` can't disagree. A file has one row however
often it fails, and a file saved since drops out of the report.

The end of a run prints one counts line, with a `✗` when anything failed and the number that
failed. Then it prints each failure: its target, its message, and its hint. It shows the first
10 failures. Then it prints `details  flnd status`.

`flnd status` shows the last run that downloaded: what it saved, then its failed targets. Each
line gives how many files failed, the kind of error, and how many runs they failed on. It also
shows the first failure's message and hint. It lists up to 10 targets, then says how many more
there are. The kinds are `network`, `blocked`, `site`, `changed`, `login`, `disk`, `tool`, `bug`
(a `PluginError`), `usage`, `gone` (a `NotFound`), and `error` for anything else.

`flnd status --json` prints the same as one JSON object for other tools. `fl.status` returns it
as a `flnd.Status` ([library.md](library.md#reading-the-home)).

### Samples of changed answers

An answer that doesn't have the shape a plugin expects (`Changed`, with the answer in
`e.response`) is kept as a sample, so a bug report can show what the site sent. It is written to
`<home>/log/<run>/samples/<n>.txt`, with the redacted URL on its first line. The failure's hint
says where the sample is. A sample is scrubbed before it is written:

- every value under a key that looks like a secret (`token`, `csrf`, `session`, `cookie`,
  `auth`, `password`, `secret`, `key`, `sig`, anywhere in the name and in any case) becomes `…`,
  in JSON at any depth, and in HTML forms and `name=value` text;
- a login's answer is never kept: one whose URL looks like a login (`login`, `auth`, `session`,
  `token`, `account`, …), or that sets a cookie;
- the file is named by a count, never by anything the site sent.

Logs and samples older than 30 days are swept at the start of a later run that downloads.

## Retrying

A retry never reuses a stored URL. The one `flnd.db` keeps is redacted, and a CDN's signed URL
expires within hours anyway. A retry works from the target, the post and the file instead. It
lists the failed post again through its plugin, starting from the cursor of the page the post
was on, so a post deep in a long listing costs one page. Then it downloads the post's files that
aren't saved, which are the ones that failed. A post the listing no longer has is recorded gone.

- **By itself.** Every `get` of a target first retries that target's failed files, before its
  new posts. A file is retried this way on at most 3 runs: its `attempts` reach 4, the first
  failure and three retries. After that it waits for `r` in `flnd target list`
  (`fl.targets.retry()`). A gone file (`NotFound`) is never retried.
- **`r` in `flnd target list`** (`fl.targets.retry(target)`) makes a target's failed files
  retryable again, however many runs failed them. The next `flnd get` lists the failed posts'
  pages and nothing else for them. A target whose whole listing failed is listed again.

A file a retry brought back is a saved event with `retried` set. The run's report counts it as
`retried`. The end-of-run line doesn't print that count yet.

## What is missing

- flnd doesn't read the browser's cookies again before it reports a 401 or 403 as a
  `LoginError`. The login error comes at once.

## Common messages

- `<target> is one post, with no categories`: a single post has no categories. Leave the
  `/categories` out.
- `not logged in`, the target's error: the plugin needs a login the account lacks. Run
  `flnd auth login <code>`.
- `<code>: not logged in, using public access; run flnd auth login <code>`: a warning. Only public
  posts are listed. Log in to get more.
- `<code>: the site refused the login (HTTP 403)`: the saved login expired or was revoked. Run
  `flnd auth login <code>`.
- `which plugin is "<text>"?`: a target with no plugin code in front and no link. Write it as
  `<code>:<text>`, with the code of a plugin.
- `got a web page instead of a photo`: the site answered a file request with a page. It is usually
  a login wall: log in again.
- `<home>/flnd.db: locked by another process, for 30s`: another flnd run held the home's database.
  Wait for it to end, or use another `--home`.
- `unexpected <Type>: …`: a bug in a plugin or in flnd. Run with `-v` and report it.
