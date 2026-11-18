# Every development task of flnd. Each recipe is one `uv` command, with no `rm`, no `&&` and no
# shell globbing, so it runs the same from bash, zsh, PowerShell and cmd, and anyone without make
# can type it by hand. uv creates and syncs `.venv` from `uv.lock` on first use.
#
#   make setup                 the environment, with the dev tools (ruff, pdoc)
#   make check                 lint, then every test: green before every commit
#   make test ARGS="-k fetch"  the tests, or some of them (unittest's own options)
#   make check PY=3.11         the same on the oldest Python `requires-python` promises

UV  ?= uv
# The Python the recipes run on. `requires-python` stays >=3.11, so run `make check PY=3.11`
# before a release to keep that floor a tested promise.
PY  ?= 3.14
RUN  = $(UV) run --python $(PY)

setup:    ; $(UV) sync --python $(PY) --all-extras
# While set, flnd refuses to read or copy a real browser profile (tests/no_profiles.py). A
# coroutine never awaited is an error, so a missed `await` fails its test.
test: export FLND_TEST_NO_PROFILES = 1
test:     ; $(RUN) python -W "error:coroutine:RuntimeWarning" -m unittest discover -s tests $(ARGS)
lint:     ; $(RUN) ruff check src flnkit tests
check: lint test
api:      ; $(RUN) python tests/api_snapshot.py --write
# The data `flnd --help` and target resolution read instead of importing every source; run it
# after changing a source's domains, routes or categories (tests/test_manifest.py checks it).
manifest: ; $(RUN) python tests/source_manifest.py --write
bench:    ; $(RUN) python tests/bench/run.py $(ARGS)
# A hand check: throughput from one real CDN host at caps 8, 16, 24 and 32, from URLs kept
# outside the repo: `make cdn-real ARGS="--urls ~/flnd-cdn.txt"` (src/docs/performance.md).
cdn-real: ; $(RUN) python tests/bench/cdn_real.py $(ARGS)
# The reference of the three public modules: the library, the source API and the test kit.
docs:     ; $(RUN) pdoc -d google flnd flnd.source flnd.testing -o build/docs
# Lists, live, what a download would save, without saving anything, in a scratch home `.try`:
# `make try ARGS="ins:example_user --only reels"`. Log in there first for a site that needs it
# (`uv run flnd --home .try login ins chrome`); `make clean` removes it.
try:      ; $(RUN) flnd --home .try download --dry $(ARGS)
# The one-file binary, from a staged copy of the two packages (tests/dist.py).
dist:     ; $(RUN) --group dist python tests/dist.py
clean:    ; $(RUN) python -c "import shutil; [shutil.rmtree(p, True) for p in ('build', 'dist', '.try')]"
.PHONY: setup test lint check api manifest cdn-real docs try dist clean
