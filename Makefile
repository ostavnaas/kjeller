.PHONY: venv clean

venv: .venv

.venv: pyproject.toml $(wildcard uv.lock)
	uv sync
	@touch .venv

clean:
	rm -rf .venv
