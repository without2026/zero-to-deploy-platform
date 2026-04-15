# Version History

SemVer. Major bumps require `BREAKING:` in the PR title and an entry in this file.

## v0.0.1-raw (2026-04-15)

- Historical snapshot of workflows and pipeline assets before the `workflow_call` refactor.
- **Not consumable** by external callers — workflows still use `on: pull_request/push` triggers.
- Purpose: establish a pre-refactor baseline for diff/audit.

## v0.1.0-alpha.2 (2026-04-15) — planned

PR 3c-2: bugs surfaced by the `ztd-smoke-hello` smoke run, fixed.

- **Bug A — `/android` path hardcode removed** from `android-tests.yml` and `android-gates.yml`. Modern AGP consumer apps have the gradle project at `project_root` directly; v3-legacy layout (`project_root/android/`) no longer assumed. Consumers with legacy layout should set `project_root: <path>/android` in `platform.yml` explicitly.
- **Bug B — core scripts fetched at runtime** via a second `actions/checkout` of this platform repo into `.ztd-platform/`. Affected jobs: `android-gates`, `electron-gates`, `web-gates`. Script calls now use `.ztd-platform/pipeline/core/*.py` and `.ztd-platform/.github/scripts/coverage-delta.py`. Step-1 migration (moving `.pipeline/core` out of consumer repos) is now wired end-to-end.
- **Gradle wrapper fallback** in `android-tests.yml`: if consumer lacks `./gradlew`, download gradle 8.7 at runtime and use it directly. Instrumented tests gracefully skip (with warning) when wrapper absent — they require on-device perms the fallback can't provide.
- Platform visibility flipped from PRIVATE → PUBLIC (GitHub Free plan does not allow cross-repo consumption of reusable workflows from private repos). Runtime data (secrets, logs, artifacts) of consumer apps remain private; only workflow DEFINITIONS become public.
- `PLATFORM_REF: v0.1.0-alpha` is currently hardcoded in the platform-checkout steps. TODO(v0.1.0): parse from `github.workflow_ref` so consumers pinning `@v1.x.y` get the matching scripts automatically.

## v0.1.0-alpha (2026-04-15)

First consumable tag for smoke testing. **Pre-release** — do not use in production.

- Three reusable workflows confirmed ready for consumption via `@v0.1.0-alpha`:
  - `ci-review.yml` — converted to dual-mode (`pull_request` + `workflow_call`)
  - `adapter-tests.yml` — already dispatcher-style workflow_call (from legacy)
  - `adapter-gates.yml` — already dispatcher-style workflow_call (from legacy)
- `workflow-call-audit` self-CI gate now shows 4 missing (was 5) — remaining:
  `ci-auto-fix.yml`, `ci-scenario-gen.yml`, `deploy-staging.yml`, `deploy-production.yml`.
  These are deferred to PR 3c-2.
- Known gaps documented in `docs/plans/e2e-readiness-trace.md` (skill repo).

## In progress — toward v0.1.0

### Step 3b (feat/3b-workflow-audit-selfci) — 2026-04-15
- **Fix** `electron-build.yml` — replaced invalid job-level `if: ... matrix.target` (actionlint expression error) with a dynamic matrix filter: a `setup` job computes the matrix JSON based on `inputs.build_target`, jobs consume via `fromJSON(needs.setup.outputs.matrix)`. This also avoids wasted CI minutes when a single target is selected (previously all 3 runners spun up and checked `if:`).
- **Fix** `ci-scenario-gen.yml` — plugged CWE-94 script-injection vector: `git push origin HEAD:${{ github.head_ref }}` → passes `github.head_ref` through an `env:` var with regex validation before `git push`. actionlint expression warning cleared.
- **Add** `.github/workflows/_self-ci-lint.yml` — platform repo's own gate. Runs on every PR + push to main: `actionlint`, yaml-load, python `compileall`, and a `workflow_call` audit that currently emits warnings (will become blocking in Step 3c).
- Discovered: `pipeline/adapters/<platform>/{build,test,gates,deploy}.yml` files are orphan (no workflow references them). Deferred cleanup to a dedicated legacy-prune PR.

### Step 3a (feat/3a-adapter-build-android) — 2026-04-15
- **Add** `.github/workflows/android-build.yml` — `workflow_call`, reads `project_root` from consumer's `.pipeline/platform.yml`, outputs `android-aab` artifact (14-day retention, sha256 sidecar).
- **Update** `.github/workflows/adapter-build.yml` — now routes `adapter=android` (previously only electron; android was silently unreachable).
- **Remove** `.github/workflows/build-mac.yml` — duplicated by `electron-build.yml`'s `macos-14` matrix target, and its `push: tags: [v*]` trigger did not fit the `workflow_call` consumer model. Any consumer that needs tag-triggered mac builds should wire their own `.github/workflows/release.yml` caller.
- Discovered: `pipeline/adapters/android/build.yml` has a hardcoded `BeCalmv3/android` path (v3 legacy) and is not actually wired into any workflow. Left as-is; flagged for cleanup in a later step.

### Later steps (planned)
- **3b** — Convert remaining per-platform workflows to `workflow_call`-only (verify no stray `pull_request` triggers), standardize job names for predictable status-check contexts.
- **3c** — Add `security-sast.yml`, `dependency-review.yml`, `sbom-sign.yml` (P0 hardening §1-3).
- **Self-CI** — `actionlint` + unit tests + fixture-roundtrip before tagging v0.1.0.

## Breaking-change policy

- MAJOR (`v1` → `v2`): input/output/secret name change, workflow removal, required-permission expansion.
- MINOR (`v1.0` → `v1.1`): new workflow, new optional input, new gate category.
- PATCH (`v1.0.0` → `v1.0.1`): rule tuning, bugfix, doc change.

Consumers pinning to `@v1` receive floating minor/patch updates automatically.
Consumers pinning to `@<sha>` are immutable until they re-pin.
