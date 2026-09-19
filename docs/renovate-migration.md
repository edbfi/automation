# Renovate-owned merging

The target invariant is: Renovate performs automatic dependency PR merges, and
GitHub prevents merging until all required CI has passed. No Actions workflow
accepts a human merge command or calls the merge endpoint.

Previously Renovate posted `/merge-when-green`, and a custom Actions helper
executed the merge. That helper also accepted maintainer `/merge` commands,
implemented a separate CI/review/DCO policy, retried default-branch CI dispatch,
and dispatched deployments. Recovery imported its internals. Removing that
control plane eliminates a second merge authority and its compensating workflow
protocols.

Renovate documents `pr-comment` as delegation to another merge bot, while `pr`
with `platformAutomerge: false` keeps merge execution in Renovate. Its normal
status evaluation is not an inventory of checks that must exist. Required checks
must therefore be declared in GitHub, not only in a repository JSON file.
[Renovate configuration](https://docs.renovatebot.com/configuration-options/#automergetype)
and [GitHub required checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging).

## Adoption sequence

Do this per repository before opting into the new automerge preset. Existing
released v2 callers are not changed by this implementation PR.

1. Disable dependency automerge in a migration PR, remove the old automerge preset
   and any later rule that re-enables it, and disable/delete the old `merge.yml`.
   Do not leave standing Renovate comments as an alternative merge path.
2. Check that full PR CI always runs and `ci / required` directly needs every
   mandatory job. Smoke must be included. The gate itself must use `if: always()`
   and may not be optional or use `continue-on-error`.
3. Configure active GitHub branch protection or a branch ruleset on the actual
   default branch (`main`, `master`, `release`, etc.). Require the actual aggregate
   display name from the jobs API, select GitHub Actions as its expected source,
   require branches to be current, and enforce the rule for administrators too.
   Neither Renovate nor another integration may bypass required checks. Requiring
   individual checks as well is fine; keep job names unique across workflows.
4. Install the [required PR policy check](pr-policy.md) alongside the aggregate.
   It preserves author-matching DCO, Conventional Commit titles, outstanding review
   requests and objections, and hold labels. Preserve native GitHub review and
   conversation-resolution rules. Verify metadata events update the current PR
   check; labels cannot be atomically bound to a merge. The retired helper's
   squash-message synthesis, comment freshness and newest-run inventory are not
   provided by these presets. A `Signed-off-by` trailer is not a cryptographic
   commit signature. Preserve trailers through the selected GitHub merge method.
5. Verify ordinary default-branch `push` CI and deployment triggers. Renovate's
   integration performs the merge, so the old `GITHUB_TOKEN` merge suppression
   workaround is no longer needed. Replace `deploy_workflows` with repository-owned
   deployment workflows gated on successful current-commit CI. Do not simply
   remove the helper if it was the only deployment trigger.
6. Upgrade related action/workflow/preset references to the same reviewed immutable
   major release. New recovery callers use `.github/repair-policy.json`; migrate
   only the existing `repair_recovery` block and `ci_workflow`. Remove obsolete
   `.github/merge-policy.json` after the old helper has been retired.
7. After protection is verified, allow automerge only for a narrowly scoped
   canary dependency PR, keeping other dependency updates opted out. Verify:
   missing, pending, failed, cancelled and
   skipped mandatory jobs prevent a merge; a changed head/base requires fresh CI;
   only complete green CI permits Renovate to merge; default-branch CI then starts.
   Keep broad automerge disabled until the canary succeeds. Finally add the new
   automerge preset, remove the canary-only rule and remove the temporary
   `automerge: false` override.

Do not publish a new major and rely on existing v2 automatic adoption to perform
these steps. v2 consumers currently auto-update the shared automation itself.
Prepare their migration configuration first, or a missing retired action may
leave their update PRs blocked. The reusable repair workflow stages its internal
action references at `v3.0.0`
so it will use the reviewed implementation when that release is published.
Those remote references cannot be exercised before publication. No release or
fleet settings change is part of this PR.

## Repository settings and validation limits

At the 2026-09-19 audit, this repository had neither branch protection nor an
active branch ruleset. This PR therefore removes its old automerge opt-in and
sets `automerge: false`, `automergeType: pr`, `platformAutomerge: false` and
`ignoreTests: false` explicitly. The base preset remains pinned to the existing
published release until the new release is available. No unverified direct
merge path is enabled.

Local tests verify preset resolution, the required aggregate, absence of the
custom merger, and recovery's inability to call mutation endpoints other than CI
dispatch. They cannot prove hosted Renovate installation permissions, required
check settings, review rules or post-merge deployment behavior. The canary above
is a release/adoption prerequisite, not a claim made by local tests.

Renovate manages its own dependency PRs. It does not provide a facility to merge
arbitrary development PRs; this repository no longer supplies one. Repository
administrators still control manual GitHub permissions and bypass policies.
