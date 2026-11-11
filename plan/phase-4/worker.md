# worker

Phase 4, step 2. `shared/_worker.py`. Stdlib only.

## What it is for

Some work is slow because it **waits**: downloading a file, calling an API, loading a page. Doing 100 of those one after another means waiting 100 times. `worker` runs several at once, so the waits overlap.

```python
# One at a time: 100 downloads × 2 s = about 200 s.
for url in urls:
    download(url)

# Eight at a time: about 25 s.
worker.map(download, urls, concurrency=8)
```

Everything else in the module is about doing that safely: a failure in one task doesn't stop the others, Ctrl-C stops cleanly, and a huge list doesn't fill memory.

## Which function to use

There are three, and each takes `concurrency=`: how many tasks run at the same time. Pick by how you want the results.

### `map`: run a function on every item, get the results back in order

```python
sizes = worker.map(file_size, urls, concurrency=8)     # sizes[i] belongs to urls[i]
```

- Use it when you have a list of inputs up front and want a list of outputs, like the builtin `map`.
- It waits until every task is done, then returns the results **in input order**.
- If any task failed, it raises `worker.TaskError` at the end, after the others finished. The error carries every result, so nothing that worked is lost:
  ```python
  try:
      sizes = worker.map(file_size, urls)
  except worker.TaskError as error:
      for result in error.results:
          if not result.ok:
              log.warn("{item}: {error}", item=result.item, error=result.error)
  ```

### `race`: get each result as soon as it's ready

```python
for result in worker.race(download, urls, concurrency=8):
    if result.ok:
        log.ok("saved {url}", url=result.item)
    else:
        log.error("{url}: {error}", url=result.item, error=result.error)
```

- Use it when you want to react to each task as it finishes, instead of waiting for all of them.
- Results come **in the order they finish**, not input order: whichever task wins the race comes first.
- Unlike JavaScript's `Promise.race`, which keeps only the first result, `race` yields **every** result. The docstring says so.
- It never raises because a task failed. Each `Result` says whether it worked.

### `pool`: add tasks while other work is still running

```python
with worker.pool(concurrency=6) as pool:
    for post in scroll_through_feed():     # still finding posts…
        pool.submit(download, post.url)     # …while earlier ones download
# leaving the block = close() + join()
print(len(pool.results), "done")
```

- Use it when you don't have the full list up front. flnd finds media while scrolling a page, and downloads each item as soon as it's found.
- `pool.submit(fn, *args)` queues one task.

**Ending it.** A pool has three steps: submit tasks, **close** it, **join** it.

| Member | What it does |
| --- | --- |
| `submit(fn, *args)` | queues one task. It waits if the queue is full (see [When things go wrong](#when-things-go-wrong)). After `close()` it raises `RuntimeError` |
| `close()` | signals the end: no more tasks are coming. Tasks already queued still run. Calling it twice does nothing |
| `join(timeout=None) -> list[Result]` | waits until every task submitted so far has finished, and returns `results`. With `timeout`, it gives up waiting after that many seconds and returns what has finished, leaving the rest running. It may be called before `close()`, for example to wait for a first batch, and more tasks can be submitted afterwards |
| `results` | the finished `Result`s, in finish order |
| `submitted` | how many tasks were queued |
| `raw` | the underlying executor |

**Two ways to use it:**
- **With `with`** (the usual way): leaving the block calls `close()` then `join()`. If the block raised, queued tasks are dropped instead of run, with a warning line.
- **Without `with`**, for code that hands the pool around, like `multiprocessing.Pool`. Call `close()` then `join()` yourself.

```python
pool = worker.pool(concurrency=6)
start_listing(on_found=lambda url: pool.submit(download, url))
...
pool.close()                 # the listing is over: nothing more is coming
results = pool.join()        # wait for the downloads still running
```

A pool that is garbage-collected without `close()` and `join()` logs `! worker.pool was never joined; N tasks may not have finished`, so a forgotten `join` is visible.

### A `Result`

| Field | Meaning |
| --- | --- |
| `item` | the input the task was given, for example the URL |
| `value` | what the function returned, or `None` if it failed |
| `error` | the exception, or `None` if it worked |
| `ok` | `True` when it worked |
| `attempts` | how many tries it took, counting retries |

## Options

All three functions take the same five options, all optional:

```python
worker.map(download, urls, concurrency=8, retries=2, timeout=30, progress="files")
worker.map(resize_image, photos, processes=True)
```

| Option | Default | What it does |
| --- | --- | --- |
| `concurrency` | about 8–32 for threads, your CPU count for processes | how many tasks run at the same time |
| `retries` | `0` | how many times to try again after a failure. It waits 1 s, then 2 s, then 4 s, and so on, never more than 60 s |
| `timeout` | none | give up on one try after this many seconds |
| `progress` | none | a label. `worker` draws a progress bar of finished tasks with that label |
| `processes` | `False` | run each task in a separate **process** instead of a thread (see below) |

### Threads or processes?

- **Threads, the default, are for waiting work:** network, files, browsers. Python runs one line of code at a time, but while one thread waits for the network, the others run.
- **Processes are for computing work:** resizing images, hashing many files, parsing huge documents. Each process has its own Python, so they compute at the same time on different CPU cores.

Processes have a catch: the function and its arguments are copied into the other process, so they must be "picklable". In practice, write the function at the top level of a module, not as a `lambda` or a function inside another function. If it can't be copied, the first `submit` says so plainly:

```text
TypeError: worker can't send <lambda> to another process: use a function defined at the top level of a module
```

### `async def` functions

If your function is `async def`, as many network libraries' functions are (Telegram's Telethon, for example), pass it as it is. `worker` runs it on an event loop in the background, at most `concurrency` at a time, and your code stays normal and synchronous:

```python
async def fetch(url): ...
pages = worker.map(fetch, urls, concurrency=10)
```

## When things go wrong

| Situation | What happens |
| --- | --- |
| A task raises an exception | That task's `Result` has the `error`, and the others keep going |
| A task fails, and `retries` is set | It waits, then tries again, up to `retries` more times |
| A try takes longer than `timeout` | That try fails with `TimeoutError`, and it's retried if retries are left. In processes and with `async def`, the try is really stopped. In threads it can't be: Python has no way to stop a thread, so the thread finishes in the background and its result is thrown away. The docstring says so |
| You press **Ctrl-C** once | No new tasks start, queued tasks and pending retries are cancelled, and the tasks already running finish. A line says how many: `! stopping: waiting for 3 running tasks (Ctrl-C again to force)` |
| You press **Ctrl-C** twice | It stops at once. Processes are terminated |
| Ctrl-C at a question asked from inside a task | It's treated the same as Ctrl-C in your main code |
| Your code inside a `pool` block raises | Queued tasks are dropped, running ones finish, and your exception goes up. A line says how many were dropped, so nothing disappears silently |
| `submit` after `close()` | `RuntimeError`: the pool said no more tasks were coming |
| An input list is huge, or never ends | Inputs are read a few at a time (twice `concurrency` ahead). `pool.submit` waits when that many are queued, so a fast producer slows down to match the workers |
| Shutting a thread or process down fails | The error is logged once, and shutdown carries on. It never hangs |

Log lines and progress bars work from inside tasks, in threads and in processes.

## How the code reads

`_worker.py` should be about 300 lines that a reader can follow from top to bottom:

1. **Public names first:** `map`, `race`, `pool` (with `submit`, `close` and `join`), `Result` and `TaskError`, each with a docstring that starts with one plain sentence saying what it does, then an example.
2. **One engine underneath.** `map` and `race` are short functions built on the same private `_Run` class that `pool` uses, so the retry, timeout, Ctrl-C and shutdown rules exist in exactly one place.
3. **Then the small helpers,** in the order `_Run` calls them: starting threads or processes, one try of one task (`_attempt`), waiting between retries, stopping.
4. **Comments explain why, not what.** A comment goes where a reader would otherwise have to work out a rule, such as why a timed-out thread can't be stopped, or why retries are cancelled on Ctrl-C. It never restates the next line.
5. **Names say what things are:** `queued`, `running`, `finished`, `stop_requested`. No `q`, `tmp`, `flag` or `data`.

The old `worker` options that almost nobody set are gone: `rate`, `setup`, `wait`, `retryable`, `errors=` and `task`. flnd's engine handles its own rate limits, browsers and retry rules.

## Tests

- **`map`:**
  - results come back in input order;
  - a failure raises `TaskError` only after the other tasks finish, and it carries every result;
  - several input lists are paired up like the builtin `map`.
- **`race`:** results arrive in finish order, every one of them (not just the first), and it never raises.
- **`pool`:**
  - submitting while tasks run;
  - `submit` waits when the queue is full;
  - `submitted` and `results`;
  - `close()` then `submit` raises, and a second `close()` does nothing;
  - `join()` before `close()` waits for the first batch, and more can be submitted after;
  - `join(timeout=…)` returns what finished and leaves the rest running;
  - leaving the block closes and joins;
  - using the pool without `with`, and the warning when it's never joined;
  - an exception in the block drops queued tasks with a warning line.
- **Processes:**
  - two tasks really run at the same time (a barrier that only opens with both);
  - a `lambda` gives the clear `TypeError`;
  - a timeout terminates the process.
- **async:** no more than `concurrency` run at once; a timeout cancels the coroutine.
- **Retries:** the waits are 1 s, 2 s, 4 s with jitter, with sleeping patched out.
- **Ctrl-C:**
  - the first press cancels queued tasks and pending retries;
  - the second stops at once;
  - Ctrl-C inside a task stops the pool.
- **Shutdown:** an error while shutting down doesn't hang.
- **Progress:** a bar with a known total, and with a growing total for `pool`.
- **Stress:** the whole file passes 60 times in a row.
