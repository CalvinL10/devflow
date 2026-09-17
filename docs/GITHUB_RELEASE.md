# GitHub release checklist

Use this checklist before sharing DevFlow on a resume. It separates repository work from
settings that must be completed on GitHub.

## Before the first public push

- Review the complete diff and split it into understandable commits.
- Confirm a clean checkout can follow the README startup path.
- Run backend lint, frontend lint/unit/build, Playwright acceptance, and the opt-in Docker
  or Compose workflow where the host supports them.
- Choose and add a license. MIT is common for portfolio code; Apache-2.0 adds an explicit
  patent grant. This is an owner decision and should not be inferred by tooling.
- Remove local databases, caches, logs, Playwright output, and runtime workspaces from the
  commit. The root `.gitignore` covers the expected paths.
- Check the staged file list for secrets and machine-specific paths.

## GitHub repository settings

- About description: `Durable human-in-the-loop code-change workflow with persisted SSE replay, constrained Docker checks, and recoverable workspace publication.`
- Topics: `fastapi`, `langgraph`, `nextjs`, `sqlite`, `docker`, `sse`,
  `human-in-the-loop`, `workflow-engine`, `playwright`, `reliability-engineering`.
- Enable private vulnerability reporting so `SECURITY.md` has a confidential channel.
- Add a 1280×640 social preview based on the workflow and architecture diagrams.
- After the first successful hosted run, add a CI badge using the actual repository URL.
- Optionally protect `main` and require the existing CI jobs. Do this only after confirming
  their check names and successful hosted execution.

## Evidence to capture

- Link one successful GitHub Actions run for the resume version.
- Create a release or tag such as `v0.1.0-portfolio` only after the corresponding commit has
  passed the chosen verification commands.
- Add one real workbench screenshot or short GIF. Show the status, timeline, and diff without
  exposing local paths, tokens, or personal data.
- Keep the README statement about CI conservative until hosted evidence exists.

## Recommended commit groups

1. Runtime/recovery changes and their backend tests.
2. Frontend behavior and Playwright/unit tests.
3. Compose dispatcher and CI workflow.
4. Portfolio documentation and repository hygiene.

Do not squash unrelated functional work into a documentation-only commit merely to make the
history look smaller; reviewers benefit from a truthful, reviewable progression.
