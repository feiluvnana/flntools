# Every development task of flnd. Each recipe is one `uv` command, with no `rm`, no `&&` and no
# shell globbing, so it runs the same from bash, zsh, PowerShell and cmd, and anyone without make
# can type it by hand. uv creates and syncs `.venv` from `uv.lock` on first use.
#
#   make setup                 the environment, with the dev tools (ruff, pdoc, pyright)
#   make check                 lint, then every test: green before every commit
#   make test ARGS="-k fetch"  the tests, or some of them (unittest's own options)
#   make check PY=3.11         the same on the oldest Python `requires-python` promises
#
# `make type` runs pyright on the public modules. It is informational: never part of `check`.

UV  ?= uv
# The Python the recipes run on. `requires-python` stays >=3.11, so run `make check PY=3.11`
# before a release to keep that floor a tested promise.
PY  ?= 3.14
RUN  = $(UV) run --python $(PY)

setup:    ; $(UV) sync --python $(PY) --all-extras
test:     ; $(RUN) python -m unittest discover -s tests $(ARGS)
lint:     ; $(RUN) ruff check flnd tests
check: lint test
api:      ; $(RUN) python tests/api_snapshot.py --write
bench:    ; $(RUN) python tests/bench/run.py $(ARGS)
# pdoc walks a package's public submodules, so `flnd` covers `flnd.source` too.
docs:     ; $(RUN) pdoc -d google flnd -o build/docs
type:     ; $(RUN) pyright flnd/__init__.py flnd/source.py
# Lists, live, what a download of one target would save, without saving or recording anything:
# `make try ARGS="ins:example_user --from reels"`. It uses the login saved in `./.flnd`.
try:      ; $(RUN) python -m flnd.dry $(ARGS)
dist:     ; $(RUN) --group dist pyinstaller --onefile --noconfirm --name flnd --specpath build --workpath build/pyinstaller flnd/__main__.py
clean:    ; $(RUN) python -c "import shutil; [shutil.rmtree(p, True) for p in ('build', 'dist')]"
.PHONY: setup test lint check api bench docs type try dist clean
