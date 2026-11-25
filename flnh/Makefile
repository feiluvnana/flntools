UV  ?= uv
PY  ?= 3.14
RUN  = $(UV) run --python $(PY)

setup: ; $(UV) sync --python $(PY)
test:  ; $(RUN) python -m unittest discover -s tests $(ARGS)
lint:  ; $(RUN) ruff check src tests
check: lint test
clean: ; $(RUN) python -c \
	   "import shutil; [shutil.rmtree(p, True) for p in ('build', '.try')]"
.PHONY: setup test lint check clean
