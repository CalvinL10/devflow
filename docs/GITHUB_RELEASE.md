# GitHub release preparation

## Candidate status

**v0.2.0-beta.1 — PENDING validation.** This is the intended beta label, not a claim
that the Git tag, GitHub release, source assets or hosted validation already exist.
Do not create or advertise the release until its actual source revision has been reviewed
and the maintainer has recorded the validation outcomes. This document does not create
new CI requirements or change repository branch protection.

## Local validation log — 2026-09-24 (not release approval)

Work continues on `cd/devflow-v0.2-beta`; no tag or Prerelease has been created.
These results describe local working-tree checks, not a successful final SHA in CI.

- PASS — backend suite excluding the opt-in Docker module: **644 passed, 6 skipped**,
  with one upstream Starlette/AnyIO deprecation warning. Command:
  `backend/.venv/Scripts/python.exe -m pytest backend/tests -q --ignore=backend/tests/test_docker_pipeline.py`.
  Platform-specific/malformed-tree skips are not treated as passes.
- PASS — separately enabled real Docker tests: **3 passed** using
  `DEVFLOW_TEST_DOCKER=1` and `DEVFLOW_RUNNER_IMAGE=devflow-candidate-runner:compose`.
  These exercise actual candidate containers and mocked model output, not a live model.
- PASS — Ruff over backend source/tests and scripts; `git diff --check`.
- FIXED, awaiting latest Linux CI — Docker local logging rejected `max-file=1`
  with implicit compression. Set `compress=false`; the speculative proxy change was
  removed. No credentials or ambient proxy variables are forwarded to downloaders.
- BLOCKED on this Windows source path — the read-only NTFS fixture bind exposes
  `0777` for committed `100644` files. Import now rejects this rather than hiding
  mode changes. Earlier mock Compose success is not evidence for the stricter importer.
  See BETA.md for the unvalidated WSL/native Linux alternative.
- NOT RUN — live Chat Completions import → plan/code/review → Docker checks → human
  approval → patch download/application. A maintainer must configure credentials
  locally; no credentials were searched for, recorded here, or added to CI.
- PENDING — final commit's hosted Actions results, clean-source startup/backup-restore
  acceptance, production-browser evidence, and public Prerelease publication.

The existing Compose CI smoke now exercises a public `colorama==0.4.6` wheel,
`src` imports without pytest path configuration, non-root/offline/read-only candidate
checks, no secret/socket mount, and actual `git apply --check`/apply. This describes
what the test checks, not its outcome; only the matching hosted run establishes that.

## Source distribution

- Distribute the reviewed source revision with `README.md`, `docs/BETA.md`,
  `compose.yaml`, `compose.demo.yaml`, both startup scripts, `.env.example`, both
  application source trees and their lockfiles, Dockerfiles and existing `LICENSE`.
- Keep the current **MIT license and copyright notice**. Do not replace it or ask users
  to choose a new license as part of release preparation.
- Exclude `.env`, local provider settings, databases, runtime volumes, backups, caches,
  node modules, local virtual environments, test reports and machine-specific paths.
- Do not package API keys, sample private code or real run exports. Review source and
  asset contents without printing credential files into terminal logs.
- No prebuilt binary, registry image or automatic updater is promised. Users build the
  source with local Docker/Compose. Initial builds require internet access.

## Validation record to complete

Record the exact revision, platform/tool versions, command and result. Leave entries
PENDING when they have not been exercised. A passing mock test is not real-provider
validation, and a local run is not evidence of a successful hosted CI run.

- PENDING — clean source startup on Windows PowerShell + Docker Desktop Linux containers.
- PENDING — clean source startup on Linux Bash + local Docker Engine.
- PENDING — real provider setup/save/test, consent, import, background task, checks,
  review, approve/reject, exported patch and manual `git apply --check`/apply.
- PENDING — deterministic demo override without a project bind or API key; separate state.
- PENDING — repository/no-HEAD and occupied-port startup diagnostics; dirty/unsupported
  source rejection by safe import preview, without source status/index refresh or filters.
- PENDING — backend has no published port or Docker socket; dispatcher has no credentials;
  original project is read-only; local-origin security remains enabled.
- PENDING — supported wheel-only dependency preparation and unsupported-source failures.
- PENDING — stop during work, restart/reconnect, persisted run history and recovery.
- PENDING — offline stop and consistent database/workspace backup, secret exclusion,
  restore into a separate installation and post-restore inspection.
- PENDING — ordinary backend tests/lint, frontend lint/unit/build and browser acceptance.
- PENDING — actual GitHub Actions run linked for this revision, where available.

Use [BETA.md](BETA.md) for the Windows/Linux commands and operator limitations. Workflow
changes belong to the CI maintainer; this checklist does not change `.github/workflows`.

## Draft release notes

> DevFlow v0.2.0-beta.1 is a source-distributed, local single-user beta candidate for
> importing clean Python Git projects, configuring a Chat Completions-compatible provider,
> reviewing isolated checks and approving/exporting changes. Real mode is the default;
> deterministic mock mode is an explicit demo override. Approval does not write to the
> original checkout. Apply the exported patch manually after independent review.
>
> Validation: PENDING. No release publication or live-provider compatibility is implied
> by this draft. See BETA.md for setup, supported dependencies, security boundaries,
> offline backup, manual recovery, and known limitations.

Replace the validation paragraph only with measured results before publishing. List
remaining limitations and known failures explicitly; do not claim broad production
readiness, universal model compatibility, or a complete hostile-code sandbox.

## GitHub publication (maintainer action)

Review the final diff and source assets; retain confidential vulnerability reporting per
`SECURITY.md`. Link actual validation evidence, then create the intended prerelease/tag
only when the maintainer chooses to publish. Mark it as a prerelease and attach only
reviewed source artifacts. Any screenshots must omit keys, private source, personal paths
and prompt data. Update release links only after the release exists. Never fabricate a
successful CI badge or a download URL for this candidate.
