export PATH := $(HOME)/.foundry/bin:$(HOME)/.local/bin:$(PATH)

.PHONY: test contracts python

test: contracts python

contracts:
	cd contracts && forge test

python:
	uv run pytest -q
