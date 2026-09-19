# Biome repair and CI recovery

Repair changes an allowlisted set of existing configuration/source files after
an actual locked Biome version change. It never merges. Normal CI still owns
frozen dependency installation and all application checks.

The compute job reads the exact same-repository Renovate head and current base,
resolves both locked Biome versions, and installs only the exact new official
Biome package in an isolated temporary directory. It validates that package's
name/version, runs migration/formatting twice and rejects non-idempotent output.
It does not install or execute dependencies from the PR. Tool processes receive
no API credentials. Schema-only updates invoke no package manager or formatter.

Publication runs trusted action code without checking out PR code. It rechecks
artifact identity, selected package/version, current base/head and allowed paths,
then creates a commit and updates the branch without force. It cannot add files,
change modes, or modify manifests, locks, workflows, hidden files or SVGs. The
new head receives an explicit guarded full-CI dispatch; an uncertain POST is
never blindly retried.

## Supported dependency evidence

`package-directory` selects an independent package directory, default `.`.
Repair currently supports **standalone text `bun.lock` only**, including a nested
standalone directory such as `frontend`. The selected manifest must agree with
its lock, which must have exactly one workspace and one stable official registry
Biome resolution. Bun workspaces, binary Bun locks, npm, pnpm and Yarn repair are
not supported. They fail closed; do not convert a repository's lockfile merely
to enable repair. Ordinary CI and smoke have broader support.

The fleet has standalone/nested text Bun consumers today. Broader privileged
repair needs a separately reviewed pure lock resolver, real immutable fixtures
and explicit package/lock-root selection. Frozen install success alone does not
prove which workspace/version publication should trust.

## App-authenticated branch updates

Use a private GitHub App installed only on intended repositories with Contents
read/write and mandatory Metadata read permission. Configure repository variable
`RENOVATE_REPAIR_APP_CLIENT_ID` and Actions secret
`RENOVATE_REPAIR_APP_PRIVATE_KEY`. The reusable repair caller provides:

```yaml
with:
  repair-app-client-id: ${{ vars.RENOVATE_REPAIR_APP_CLIENT_ID }}
  # Also provide explicit config-files, source-roots, package-directory and bun-version.
secrets:
  repair-app-private-key: ${{ secrets.RENOVATE_REPAIR_APP_PRIVATE_KEY }}
```

Only publication receives the key. Its short-lived token is scoped to the current
repository and Contents write and is revoked at job completion. Only the non-force
branch update uses that token; reads, object creation and dispatch use the workflow
token. There is no `GITHUB_TOKEN` fallback for the branch update. This avoids the
approval-required PR workflows observed after workflow-token updates.

Authorship and DCO remain `github-actions[bot]`; the base preset's narrow
`gitIgnoredAuthors` entry lets Renovate handle those repair commits without
ignoring human changes. Both regular PR CI and explicit dispatch validate the
complete app. Callers accept `pr-number` and `expected-head-sha` dispatch inputs
and run `actions/dispatch-guard` in validation and the aggregate.

## Optional recovery

Keep recovery only where interrupted publication/dispatch still needs it. It
uses `.github/repair-policy.json`, independently of merge policy:

```json
{
  "schema": 1,
  "ci_workflow": "ci.yml",
  "repair_recovery": {
    "enabled": true,
    "automation_ref": "v2.0.0",
    "package_directory": ".",
    "config_files": ["biome.json"],
    "source_roots": ["src", "tests"]
  }
}
```

Use the exact installed repair release for `automation_ref`; the example also
allows recovery of existing v2 publications. Consumers must upgrade action and
workflow references consistently when adopting a new release. Copy the
[recovery workflow](../.github/workflows/repair-recovery.yml), replacing its local
checkout/action steps with the immutable released recovery action. Its run name,
dispatch inputs and repository-wide concurrency group are protocol fields.

Recovery only executes the current trusted default branch. It verifies repository
identity, PR/base/head, review objections, publication workflow/ref/run/attempt,
publication log, parent and allowlisted diff. Author text alone is insufficient.
The default policy here disables recovery because this repository has no active
repair caller.

A reservation in Actions history precedes CI dispatch. At most two reservations
per repaired head can dispatch; reruns and uncertain responses consume the same
bounded budget. Five-minute grace periods let accepted runs appear. Active CI
waits, deterministic failures block, and only proven missing dispatch or the
specific approval-required zero-job case can retry. Recovery does not approve
workflows or manufacture merge requests. Retain publication logs for seven days
and Actions history for at least 30 days; unavailable proof blocks safely.
