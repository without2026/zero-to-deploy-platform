# Version History

SemVer. Major bumps require `BREAKING:` in the PR title and an entry in this file.

## v0.0.1-raw (2026-04-15)

- Historical snapshot of workflows and pipeline assets before the `workflow_call` refactor.
- **Not consumable** by external callers — workflows still use `on: pull_request/push` triggers.
- Purpose: establish a pre-refactor baseline for diff/audit.

## Planned — v0.1.0

- Refactor all consumer workflows to `on: workflow_call:` with typed inputs and explicit secret declarations.
- Consolidate `android-*.yml`, `electron-*.yml`, `web-*.yml` (9 files) into matrix-driven `adapter-*.yml` (3 files).
- Add `security-sast.yml`, `dependency-review.yml`, `sbom-sign.yml` (P0 hardening §1-3).
- Add `.semgrep/` shared ruleset.
- Self-CI: `actionlint` + unit tests + fixture-roundtrip.

## Breaking-change policy

- MAJOR (`v1` → `v2`): input/output/secret name change, workflow removal, required-permission expansion.
- MINOR (`v1.0` → `v1.1`): new workflow, new optional input, new gate category.
- PATCH (`v1.0.0` → `v1.0.1`): rule tuning, bugfix, doc change.

Consumers pinning to `@v1` receive floating minor/patch updates automatically.
Consumers pinning to `@<sha>` are immutable until they re-pin.
