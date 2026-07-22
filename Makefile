# Developer convenience targets. See CONTRIBUTING.md.

.PHONY: test lint type check docs build clean

test:
	uv run pytest

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

type:
	uv run mypy src/

check: lint type test

# Regenerate the four customer-facing .docx files from their markdown
# sources. Run after editing any docs/*.md.
docs:
	@for f in technical-reference operations-runbook anaplan-model-setup-guide developer-guide; do \
		pandoc --from=gfm+yaml_metadata_block --toc --toc-depth=2 \
			-o docs/$$f.docx docs/$$f.md && echo "regenerated docs/$$f.docx"; \
	done

build:
	rm -rf dist/
	uv build

clean:
	rm -rf dist/ .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage
