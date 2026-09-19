# Release-age containment

Verified 2026-09-19. Mend's hosted installation reports Renovate 44.103.0.
The repository's Renovate devDependency does not select that hosted engine.

## Upstream boundary and reproduction

[Discussion #45284](https://github.com/renovatebot/renovate/discussions/45284)
tracks the missing post-artifact pending-version safeguard for Bun, npm and
Gradle. [Exploratory PR #45278](https://github.com/renovatebot/renovate/pull/45278)
is closed and unmerged; it is not a shipped fix. The latest published release
observed during this review was 44.103.2.

The credential-free reproduction uses the published 44.103.0 engine's real
`getUpdatedPackageFiles()` and Bun 1.4.2 artifact command, with local Git reads
supplied by the harness. Its immutable input is the
[upstream fixture at 182c40f8](https://github.com/risu729/renovate-package-reextract-repro/tree/182c40f8bf9b1a16d4a632b06438ef4bdc5307f7).
Node 24.14.0 and automation's locked harness dependencies at
`7aa09782f21e88c17ead8b12b38ae6861eda4542` were used; this is an engine-path
reproduction, not a claim to reproduce Mend's complete execution environment.

| Manifest update | Selected eligible version | Artifact version | Result |
| --- | --- | --- | --- |
| `>=4.120.1 <=4.123.0` | Wrangler 4.120.1 | 4.123.0, explicitly pending in the fixture | Warning, no Artifact Error |
| Exact `4.120.1` | Wrangler 4.120.1 | 4.120.1 | Eligible resolution; the upstream warning remains |

The pending list is fixed by the historical fixture, not by today's age of
those releases. Both cases execute the real package manager without repository
credentials. The downloaded engine tarball's npm integrity was verified.
Clearing a dashboard warning does not establish that the engine is fixed.

## Fleet impact and containment

The effective shared policy requires three days of release age for npm-backed
dependencies. The audit checked actual artifact-selected **direct** dependencies,
including npm alias identity, against official registry publication timestamps
and integrity. It did not impose an age rule on every transitive package.

Four direct entries in the initial default locks were younger than 72 hours:

| Consumer | Installed version at audit | Age at audit | Eligible correction |
| --- | --- | --- | --- |
| Obzorarr | `@lucide/svelte@1.47.0` | 52.7 hours | 1.46.0 |
| Obzorarr | `motion@13.4.0` | 70.9 hours | 13.3.0 |
| Zondarr | `@lucide/svelte@1.47.0` | 52.7 hours | 1.46.0 |
| Zondarr | `jsdom@30.1.0` | 59.1 hours | 30.0.1 |

These are observations of the default locks; they do not identify which prior
operation introduced each version. The corrections and exact pins are tracked
in [Obzorarr #203](https://github.com/edbfi/obzorarr/pull/203) and
[Zondarr #239](https://github.com/edbfi/zondarr/pull/239).

[Guides #48](https://github.com/edbfi/guides/pull/48),
[Portaler #94](https://github.com/edbfi/portaler/pull/94),
[Setun #67](https://github.com/edbfi/setun/pull/67), and
[Poyo #62](https://github.com/edbfi/poyo-studio/pull/62) pin their existing direct
resolutions. All six consumers retain exact versions with scoped Bun/npm
`rangeStrategy: pin` rules. Other active scoped consumers already use exact
versions. Otpravkarr remains locked and opted out for its separate legacy-writer
blocker. Unenrolled repositories and owner-excluded applications are not silently
enrolled by this audit.

Across the nine compatibility/containment candidates, all 251 direct dependency
entries have exact manifest/lock agreement, matching official npm integrity, and
publication ages of at least 72 hours. Fresh frozen installs and full application
validation accompany the PRs. Final-head required CI and merged-default health
remain separate acceptance requirements; follow each PR for its actual status.

This containment keeps the selected direct version fixed through artifact
resolution. Preserve those exact pins when adding or changing dependencies.
Renovate remains the sole ongoing dependency merger; its stability checks,
strict source-pinned required CI, DCO, review requirements, holds, schedules and
manual updater ownership remain in force. No merge writer or shared lock resolver
was added, and privileged Biome repair support is unchanged.

## Conditions for revisiting containment

A future upstream fix must reject the range reproduction's pending artifact
version, preserve the exact-pin control, and run in the actual hosted
installation before the safeguard can be called fixed. Manifest-only extraction
that merely removes the warning is insufficient: the resolved lock version is
the evidence that matters. Review any proposed return to ranges against that
behavior. A broader shared verifier requires its own RFC, interface comparison,
explicit format boundary and positive/negative hosted enforcement evidence.
