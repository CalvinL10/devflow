# Contributing to DevFlow

DevFlow is currently a single-user portfolio MVP, but focused bug reports and small,
well-tested pull requests are welcome.

## Development setup

Use Python 3.11, Node.js 22, `uv`, npm, Docker with Linux containers, and Docker Compose.
The exact local and Compose startup commands are maintained in `README.md`.

## Verification matrix

Run the checks relevant to your change before opening a pull request:

```bash
cd backend
uv sync --locked --python 3.11
uv run --locked ruff check --no-cache src tests
uv run --locked pytest -q -p no:cacheprovider tests

cd ../client
npm ci
npm run lint
npm test
npm run build
npm run test:e2e
```

The opt-in Docker pipeline test and full Compose workflow require a running Docker daemon;
see `README.md` for those commands. On Windows, run the backend suite from a location where
pytest can create and secure its temporary directories.

## Pull requests

- Keep claims and documentation aligned with implemented behavior.
- Add or update tests for behavior changes.
- Preserve existing authentication, validation, isolation, and container restrictions.
- Do not add hash schemes, frozen contracts/schemas, baselines, or quality gates without a
  documented concrete failure scenario and a demonstrated reason ordinary versioning,
  constraints, types, transactions, and tests are insufficient.
- Call out changes to trust boundaries, Docker access, persistence, or recovery semantics.
