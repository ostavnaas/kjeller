PIPENV ?= uvx pipenv
PYTHON ?= 3.12

export PIPENV_VENV_IN_PROJECT := 1

.PHONY: venv clean

venv: .venv

.venv: Pipfile.lock
	$(PIPENV) sync --dev --python $(PYTHON)
	@touch .venv

clean:
	rm -rf .venv
