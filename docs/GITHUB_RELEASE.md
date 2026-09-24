# GitHub release preparation

## Candidate status

**v0.2.0-beta.1 — PENDING validation.** This is the intended beta label, not a claim
that the Git tag, GitHub release, source assets or hosted validation already exist.
Do not create or advertise the release until its actual source revision has been reviewed
and the maintainer has recorded the validation outcomes. This document does not create
new CI requirements or change repository branch protection.

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
