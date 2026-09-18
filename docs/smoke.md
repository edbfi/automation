# Application smoke support

A smoke test must start the application and assert that something meaningful
loads. Build success and HTTP readiness alone do not establish that a UI renders.
Keep the assertions with the application: visible browser landmarks/interactions
for client apps, or expected HTML/API content for server-rendered apps and static
docs. Native, extension and container apps keep their own launch mechanisms.

`actions/smoke` is a composite action for Linux/macOS with Python 3. It owns one
foreground app process, bounded loopback HTTP(S) readiness, repository-owned
assertions, failure output and process-group cleanup. The caller owns dependency
installation, builds, environment, browser installation and temporary databases.
No package manager or lockfile is assumed.

```yaml
# After checkout, the repository's frozen install and production build:
- uses: edbfi/automation/actions/smoke@RELEASE
  with:
    start-command: bun run preview --host 127.0.0.1 --port 4173
    url: http://127.0.0.1:4173/
    test-command: bun run smoke:assert
    working-directory: frontend
    timeout-seconds: 60
    test-timeout-seconds: 120
```

Replace `RELEASE` with an immutable release containing this action. Both commands
run in `working-directory` and inherit caller environment. The start command
must remain in the foreground; neither command should detach into another
session. A pre-existing listener is rejected so a stale server cannot satisfy
the test. Readiness requires a 2xx response without following redirects, with
network and HTTP failures retried until the startup deadline. Choose a concrete
ready endpoint such as `/health` or `/index.html` if `/` redirects.

`test-command` is mandatory. It runs only after readiness and must exit nonzero
on an incorrect result. It gets its own deadline. An app exit before or during
assertions fails the action. Both process groups are terminated on success,
failure, timeout or handled cancellation, including descendants left by shells.
Failure output includes both the app and assertion logs. A hard runner shutdown
cannot execute cleanup; GitHub must dispose of that runner.

For docs with no package manifest or lockfile:

```yaml
- uses: edbfi/automation/actions/smoke@RELEASE
  with:
    start-command: python3 -m http.server 4321 --bind 127.0.0.1 --directory docs
    url: http://127.0.0.1:4321/index.html
    test-command: bash .github/scripts/assert-pages.sh
```

Put content assertions in `assert-pages.sh`. Avoid `curl | grep -q` under
`pipefail`: an early successful grep can close the pipe and fail curl. Write the
response to a temporary file, then assert on it. Prepare a build artifact in the
same run before downloading it; a standalone `workflow_dispatch` must build its
own artifact or select and validate an explicit producer.

Use a `smoke` job called directly from full CI and include it in the final gate:

```yaml
required:
  name: ci / required
  if: always()
  needs: [quality, build, smoke]
  runs-on: ubuntu-24.04
  steps:
    - uses: edbfi/automation/actions/gate@RELEASE
      with:
        needs: ${{ toJSON(needs) }}
        required: quality build smoke
```

Require this aggregate in GitHub before enabling Renovate automerge. A missing,
skipped or unsuccessful smoke job then fails the aggregate.

Existing Playwright `webServer` configurations already own app lifecycle and
usually need no migration. Do not wrap a second startup command around them.
The [coverage review](review-2026-09-19.md) distinguishes HTTP readiness, semantic
HTML/API checks and browser/native execution rather than treating them as equal.
