# Pull request policy

Require `ci / policy` alongside `ci / required` in GitHub protection, with GitHub
Actions as the expected source. The policy action only reads GitHub metadata; it
cannot merge, push, comment or publish checks. The Actions job supplies its own
ordinary check result.

Every PR must have a Conventional Commit title and an exact author-matching
`Signed-off-by: Name <email>` line in the final paragraph of every commit message.
Renovate PRs must also use a same-repository `renovate/` branch and contain an
authenticated Renovate-authored commit. This is
DCO evidence, not a cryptographic signature. The action does not synthesize squash
messages; choose repository merge settings that preserve required commit trailers.

The check rejects draft/closed PRs, `manual-dependencies` and `do-not-merge` labels,
outstanding requested reviewers/teams, visible pending reviews and unresolved
changes requests. Unlike the old dependency-only label gate, holds apply to all
PRs. An optional approval count accepts only the latest approval per reviewer on
the current head, excluding the PR author. All existing fleet policies used zero;
retain stronger native GitHub review requirements independently.

A consumer installs this wrapper, replacing `RELEASE` with the reviewed immutable
release. The shared workflow runs only the released policy action and checks out
no application code:

```yaml
name: PR policy
on:
  pull_request:
    types: [opened, reopened, synchronize, edited, labeled, unlabeled, ready_for_review, converted_to_draft, review_requested, review_request_removed]
  pull_request_review:
    types: [submitted, edited, dismissed]
permissions:
  contents: read
  pull-requests: read
concurrency:
  group: pr-policy-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  policy:
    uses: edbfi/automation/.github/workflows/pr-policy.yml@RELEASE
```

Read the actual check display name from the jobs/checks APIs when installing
protection: reusable workflow callers may add a job-name prefix. Use one unique
policy job and require its exact emitted name. The separate metadata workflow
must remain required independently of the ordinary CI aggregate, so a label or
review change refreshes policy without rerunning the application's full suite.

The action verifies the exact event head, complete commit pagination, then
re-reads the head/base, labels, title, review requests and reviews before success.
Per-PR concurrency cancels older metadata evaluations across both event types.
Keep the wrapper and pinned references under normal code review.

Use `pull_request`, not `pull_request_target`: the latter's default-branch check
does not satisfy a required PR-head context. GitHub attaches PR/review checks to
the PR merge revision. See [GitHub event semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
and [required-check troubleshooting](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).

GitHub delivers metadata events asynchronously. A label/review change and merge
are not one atomic transaction, and another user's unpublished private review
may not be visible to the token. Native GitHub review protection remains the
server-side review gate. Before direct automerge opt-in, a hosted canary must
prove that a post-green hold and submitted/dismissed review produce the expected
blocking/refreshed current-PR check. Any stronger hold guarantees need a separately
verified platform policy; this action does not claim atomic label enforcement.

This repository bootstraps its implementation with a local read-only action in
`policy.yml`, like its local required-CI gate. Consumers use the immutable released
action through `pr-policy.yml`; no unreleased remote reference runs in the
implementation PR's own policy check.

A branch updated only with `GITHUB_TOKEN` may suppress the PR event. Dispatching
`ci.yml` alone does not create this separate policy result. Keep that head blocked
until a supported Renovate/App update triggers complete PR CI and policy. Current
Biome publication uses an App token; verify both required contexts on its hosted
canary. Recovery never manufactures a policy result or bypasses missing checks.
