# Automation

Reusable validation workflows, Renovate presets and app smoke support.
Licensed under [AGPL-3.0-only](LICENSE).

Renovate is the sole automatic PR merger. It merges dependency PRs itself, after
required CI succeeds. Actions validate code, publish bounded Biome repairs and
recover missing CI; they do not merge PRs. This repository does not automate the
merging of development PRs.

**The current branch contains an unreleased breaking migration from v2.** Read
[the migration guide](docs/renovate-migration.md) before adopting it. Existing
immutable releases retain their behavior. This repository's dependency automerge
is disabled until required-check protection and the migration canary are verified.

## Repository-owned CI

The `bun`, `python`, `go`, `rust`, `zig` and `content` reusable workflows set up
runtimes and run an explicit `command` in `working-directory`. Callers supply
runtime `version`, and may override `runner` and `timeout-minutes`. Keep native
builds, service dependencies, database setup and browser configuration in the
repository that owns them. Every workflow rejects tracked-file mutations.

The Bun workflow installs frozen dependencies. `install-directory` defaults to
`working-directory`; set it separately when checking a workspace member:

```yaml
with:
  version: 1.4.2
  install-directory: .
  working-directory: packages/web
  command: bun run check
```

The install directory must contain `package.json` and exactly one `bun.lock` or
`bun.lockb`. Only that lock contributes to the package-cache key. A nested
independent application uses its own install directory. The workflow does not
infer another package manager or regenerate missing locks. npm, pnpm and Yarn
projects should own their frozen install steps and can use the runtime-neutral
[smoke action](docs/smoke.md) and gate afterward. Python callers own their frozen
`uv sync`/lock checks; setup-uv resolves and logs `latest` stable uv.

Run full CI on every pull request, default-branch push and explicit dispatch.
Use one final `ci / required` job with `if: always()` that directly needs **every**
mandatory job, including smoke. Pass its full `toJSON(needs)` and the exact job
IDs to `actions/gate`. Missing, undeclared, failed, cancelled, pending, neutral
and skipped prerequisites all fail the gate. Do not filter the aggregate by
paths or dependency type.

Require that aggregate in GitHub branch protection/rulesets, bind it to the
GitHub Actions app and require branches to be current. Do not give Renovate a
bypass. GitHub itself accepts skipped/neutral checks; the always-running gate
turns those prerequisite results into failure. See the complete wiring in
[this repository's CI](.github/workflows/ci.yml).

## Dependency updates

Use immutable full-version references for presets, actions and workflows. The
base `default.json` keeps automerge disabled. Add `mixed.json` to separate tested
ecosystems, and add `automerge.json` **only after** the migration prerequisites
are satisfied. Replace `RELEASE` with the published release you have validated:

```json
{
  "extends": [
    "github>edbfi/automation//default.json#RELEASE",
    "github>edbfi/automation//automerge.json#RELEASE"
  ]
}
```

The optional automerge preset makes all update types eligible, including majors
and pre-1.0 updates. It uses `automergeType: pr`, `platformAutomerge: false` and
`ignoreTests: false`. Never turn off test checking. Renovate rebases behind-base
branches. Required CI remains enforced by GitHub even if another check finishes
before CI starts. Explicit later repository package rules may disable automerge.

The base retains bot-authored DCO trailers, release-age checks, separate
TypeScript/Biome/prek/Actions groups, exact installed Biome versions and lockfile
maintenance. Preserve deliberate boundaries where another updater owns a package.
The [migration guide](docs/renovate-migration.md) explains retired comment commands,
review/DCO policy, deployment triggers and safe rollout.

## App smoke and repair

Use [shared smoke support](docs/smoke.md) to start an app, wait for readiness, run
repository-owned assertions and clean up its process tree. HTTP readiness alone
is not a rendering test. Existing Playwright `webServer` setups can stay local.
The [fleet review](docs/review-2026-09-19.md) records current coverage and gaps.

[Biome repair and recovery](docs/repair.md) remain separate from merging. Repair
uses a scoped GitHub App for branch updates and has deliberately narrower lock
support than ordinary CI. Unsupported lock formats/workspaces fail closed.

## Maintenance

Use focused Conventional Commits with genuine DCO sign-offs. Validate locally:

```sh
bun install --frozen-lockfile --ignore-scripts
bun run prepare:native
bun run test
bun run validate:renovate
actionlint
```

RE2 must load natively; tests do not accept a JavaScript regex fallback. The test
suite covers resolved Renovate policy, frozen Bun text/binary installs and
workspaces, strict aggregation, real Biome migration, publication races, bounded
recovery and smoke process lifecycle failures.

Publish an immutable release only after the implementation PR and merged revision
pass CI. This migration needs a major release and coordinated consumer adoption;
do not move existing tags or publish it as a compatible v2 update.
