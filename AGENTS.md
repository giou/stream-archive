# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project

StreamArchive: Twitch and Kick stream monitor, recorder, and YouTube restreamer.
Python 3.14, src layout (`src/stream_archive`), managed with uv.

## Documentation style (mandatory)

**Always use the `simple-english` skill when writing or editing any human-readable
prose**, including:

- README and all Markdown docs
- Release notes / changelog entries
- Code comments
- Docstrings
- Commit messages, PR descriptions, error messages, and any other user-facing text

Read `skill://simple-english` first, then apply its rules: short sentences,
active voice, simple tenses, one word one meaning, no filler.

## Commands

```sh
uv run pytest          # tests
uv run ruff check      # lint (line length 120, E501 ignored)
uv run ruff format     # format
uv run mypy            # type check (strict)
```

The hold/reuse recorder tests spawn `ffmpeg`; install it before you run the suite.

## CI gates

CI runs on every push and PR (`ci.yml`). The `test` job runs, in order:
`uv sync --locked`; a check that the vendored streamlink-ttvlol plugin matches
its Dockerfile digest; a check that the workflow's uv version and the
Dockerfile uv stage agree; a check that the Dockerfile healthcheck port equals
`_HEALTH_PORT`; `ruff format --check .`; `ruff check .`; `mypy`; then
`pytest -q`. Treat these as the definition of done. `--locked` means dependency
changes require a matching `uv.lock` update. The image build uses `--frozen`,
because it installs the shipped lock as it is.

A second job, `image`, builds the container image, but only on a Dependabot
pull request: nothing else in CI builds it, and a base-image bump should fail
before the merge instead of at release time.

## Conventions

- Run `ruff format`, `ruff check`, and `mypy` on changed code before yielding.
- `.pre-commit-config.yaml` runs `ruff --fix`, `ruff-format`, and `mypy` (scoped to `src/stream_archive`) at commit time.
- Tests live in `tests/`; match existing test style.
