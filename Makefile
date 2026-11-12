TASKS := setup test lint type check flnd dist
.PHONY: $(TASKS)

$(TASKS):
	uv run --no-project scripts/dev.py $@ $(ARGS)
