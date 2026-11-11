PYTHON ?= 3.14

.PHONY: setup test lint typecheck check flnd dist

setup:
	uv venv .venv --python $(PYTHON)
	uv pip install -e '.[archive,selector,browser,bundler]' --group dev --python .venv/bin/python
	.venv/bin/playwright install chromium

test:
	.venv/bin/python -m unittest discover -s tests

lint:
	.venv/bin/python -m compileall -q flnd shared

typecheck:
	.venv/bin/pyright

check: lint typecheck test

flnd:
	.venv/bin/flnd $(ARGS)

dist:
	.venv/bin/python -c "from shared import bundler; bundler.build('flnd/__main__.py', name='flnd', data=('licenses',))"
