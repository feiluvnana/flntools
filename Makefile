TASKS := setup test lint type check api flnd dist try
.PHONY: $(TASKS)

$(TASKS):
	uv run --no-project scripts/dev.py $@ $(ARGS)
