# worker

Phase 3. Depends on [log](../phase-1/log.md), which must follow its [Threads](../phase-1/log.md#threads) rules. Stdlib only (`threading`, `queue`, `concurrent.futures` for processes).

[`shared/_worker.py`](../../shared/_worker.py). Run work in parallel with one call. The default runs tasks in threads, which suits I/O such as HTTP, files, and browsers. `processes=True` is for CPU-heavy work. On a free-threaded Python (3.14t), threads also run CPU work in parallel.

There are three ways to use it. All three share the same options and the same rules.

```python
# 1. map: like the builtin map, but parallel. Results come back in input order.
sizes = worker.map(fetch_size, urls, workers=8)
worker.map(save, urls, dests, workers=8, retries=2)  # several iterables are zipped

# 2. each: yields a Result as soon as each task finishes, in finish order
for result in worker.each(fetch, urls, workers=8):
    if result.ok:
        log.ok("{item}", item=result.item)

# 3. pool: submit work while other work runs, e.g. download while still listing
with worker.pool(workers=6, retries=2, progress="files") as pool:
    for item in list_items():
        pool.submit(save, item.url, item.dest)
# leaving the block waits for every task
```

The decorator form is the same thing with the options attached to the function:

```python
@worker.task(workers=8, retries=2, rate=5)
def save(url: str, dest: Path) -> bool: ...

save(url, dest)        # still a plain call, in the current thread
save.map(urls, dests)  # worker.map(save, urls, dests, workers=8, retries=2, rate=5)
save.each(urls, dests)
```

## Options

Every entry point takes these options as keywords.

| Option | Default | Meaning |
| --- | --- | --- |
| `workers` | `min(32, cpu + 4)` for threads; `cpu` for processes | how many tasks run at once |
| `retries` | `0` | extra attempts after a task raises |
| `backoff` | `1.0` | first retry wait in seconds. Each later wait doubles, with ±25 % jitter, up to 60 s |
| `retryable` | `(Exception,)` | only these exception types are retried |
| `rate` | `None` | at most this many task starts per second, shared by all workers. A token bucket, for API limits |
| `timeout` | `None` | seconds per attempt. A task past it fails with `TimeoutError` (see [Timeouts](#timeouts)) |
| `progress` | `None` | a label. Draws a `log.progress` bar of finished tasks |
| `errors` | `"raise"` | `"raise"` or `"log"` (see [Errors](#errors)) |
| `setup` | `None` | a per-worker resource (see [Per-worker resources](#per-worker-resources)) |
| `processes` | `False` | use processes instead of threads |

A bad option raises `ValueError` before any task starts: `workers < 1`, `retries < 0`, `rate <= 0`, `setup` together with `processes=True`, or an unknown `errors` value.

## Results

`each` and `pool.results` give `Result` objects:

```python
result.item      # the arguments the task was given: one value, or a tuple when several iterables were zipped
result.value     # the return value, or None after a failure
result.error     # the last exception, or None
result.ok        # error is None
result.attempts  # 1 + the number of retries used
result.seconds   # wall time across all attempts
```

`map` returns plain values in input order.

A pool keeps live counters that any thread can read: `pool.submitted`, `pool.running`, `pool.succeeded`, and `pool.failed`. `pool.results` lists the finished `Result`s in finish order. A producer can use the counters to stop early, for example `if pool.submitted >= limit`.

## Errors

A failing task never stops the others. Once all tasks have finished, what happens next depends on `errors`:

- **`"raise"`** (the default): if any task failed, `worker.TaskError` is raised. It carries `failed: list[Result]` and `results: list[Result]`, so the work that succeeded is not lost. Its message gives the count and the first error, for example `3 of 120 tasks failed; first: ins:abc: HTTPStatusError 404`. `map` raises it. `pool` raises it when its block exits. `each` never raises, because you already see every `Result`.
- **`"log"`**: each failure is written with `log.error("{item}: {error}")` when it happens, and nothing is raised at the end. This suits downloads, where one bad file should not fail the run.

`retryable` is checked before each retry. `KeyboardInterrupt` and `SystemExit` are never retried and never stored as a result.

## Ctrl-C and shutdown

- **The first Ctrl-C:**
  1. stops taking new input;
  2. drops queued tasks;
  3. waits for the tasks already running;
  4. runs the `setup` teardown;
  5. raises `KeyboardInterrupt`, which `cli` turns into exit `130`.

  While it waits, a `log.warn("stopping: waiting for {n} running tasks (Ctrl-C again to force)")` line is shown.
- **A second Ctrl-C** stops waiting and raises at once. Running threads are daemon threads, so they cannot keep the process alive.
- **When the code that called `map`, or the body of a `pool` block, raises:** queued tasks are dropped, running tasks finish, and the exception propagates.

## Timeouts

A Python thread cannot be killed. When a thread task passes `timeout`, it is marked failed with `TimeoutError` and the pool stops waiting for it. The thread keeps running in the background until its function returns, and its result is discarded. Its worker slot is replaced, so the pool does not shrink. This is the honest limit of threads, and the docstring says so. For a real per-request limit, use the library's own timeout (httpx and Playwright both have one); `timeout` here is a safety net.

With `processes=True`, a task past its timeout is also marked failed, and its process is replaced once it returns.

## Input and backpressure

Input iterables are read lazily. At most `workers * 2` tasks wait in the queue, so `worker.map(save, read_huge_file())` never loads the whole input into memory. `pool.submit` blocks when the queue is full, which slows a fast producer, such as a listing loop, down to the speed of the workers.

When the input has a length (`len()` works) and `progress` is set, the bar has a known total. Otherwise, and for `pool`, the total grows as tasks are submitted.

## Per-worker resources

Some resources should not be shared between threads. A Playwright browser must only be used by the thread that created it. Some resources are simply expensive to create per task.

```python
with worker.pool(workers=3, setup=lambda: browser.open(headless=True)) as pool:
    for url in urls:
        pool.submit(screenshot, url)

def screenshot(page, url):  # the resource comes first
    page.goto(url)
    ...
```

- `setup` is a function with no arguments. It returns either a context manager, which is entered, or a plain value.
- Each worker thread calls `setup` once, before its first task. The task function then gets the resource as its first argument.
- The context manager is exited when the pool shuts down, **in the same thread that entered it**. Playwright requires this, so the pool runs its own worker threads instead of `ThreadPoolExecutor`.
- If `setup` raises, that worker records the error for every task it would have taken. The other workers keep going. The setup error is reported once, not per task.

`setup` is threads-only. With `processes=True` it raises `ValueError`, because the resource could not cross processes.

## Processes

`processes=True` uses `concurrent.futures.ProcessPoolExecutor` with the same options and results. The task function and its arguments must be picklable: a module-level function, not a lambda. The pool checks this on the first submit and raises `TypeError` with a clear message, instead of letting a pickling error surface from deep inside the executor. Processes pay off only for CPU-heavy tasks; for I/O, threads are cheaper.

## Nesting

A task may use `worker` itself. Each pool has its own threads, so an outer pool of 2 targets with an inner pool of 6 downloads each means up to 12 downloads at once. The total is the product, which is intended and written in the docstring. There is no global thread limit.

## Tests

- `map`:
  - input order;
  - several iterables;
  - an empty input;
  - a lazy generator that is never fully loaded (count how much is read ahead);
  - `TaskError` keeps the partial results.
- `each`: yields in finish order and never raises.
- `pool`:
  - `submit` while tasks run;
  - blocking when the queue is full;
  - waits on exit;
  - raises `TaskError` on exit;
  - an exception in the body drops queued tasks.
- The decorator: a plain call, `.map`, `.each`, and the options carried over.
- Retries:
  - only `retryable` types;
  - `attempts` counts;
  - backoff waits, with sleep patched;
  - `KeyboardInterrupt` is never retried.
- `rate`: starts per second stay within the limit, measured with a fake clock.
- `timeout`: the result is `TimeoutError` and the worker slot is replaced.
- `errors="log"` writes a line per failure through a fake `Screen` and does not raise.
- `progress`: a known total and a growing total.
- `setup`:
  - called once per worker;
  - exited in the same thread (check `threading.get_ident()`);
  - exited on Ctrl-C and on error;
  - a failing `setup` is reported once.
- Ctrl-C, simulated by raising `KeyboardInterrupt` in the caller: queued tasks are dropped and running tasks finish.
- `processes=True`: a CPU-bound task runs faster than in one thread on a machine with 2 or more CPUs (skipped otherwise); a lambda gives the clear `TypeError`.
- Option validation errors.
