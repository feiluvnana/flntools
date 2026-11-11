PYTHON ?= 3.14

.PHONY: setup test lint typecheck check flnd dist

setup:
	uv venv .venv --python $(PYTHON)
	uv pip install -e '.[archive,selector,browser,bundler,media]' --group dev --python .venv/bin/python
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
	.venv/bin/python -c "$$FFMPEG_SOURCE" > dist/flnd/licenses/ffmpeg-source.txt

# The exact ffmpeg in the bundle, for the GPL source offer in licenses/ffmpeg.txt.
export FFMPEG_SOURCE = import subprocess, imageio_ffmpeg; \
	print('ffmpeg-source\nThe ffmpeg program in this bundle, as it reports itself. The source offer is in ffmpeg.txt.\n'); \
	print(subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-version'], capture_output=True, text=True).stdout)
