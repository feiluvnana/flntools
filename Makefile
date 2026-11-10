.PHONY: install test lint run

install:
	pip install -e .

test:
	python -m unittest discover -s tests

lint:
	python -m py_compile flnd/**/*.py
