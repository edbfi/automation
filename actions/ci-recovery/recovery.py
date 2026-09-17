"""Recover proved Biome publications; reserve attempts in trusted Actions history."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge = load('checked_merge', 'merge/merge.py')
repair = load('biome_repair', 'biome-repair/repair.py')
require, Blocked, sha, timestamp = merge.require, merge.Blocked, merge.sha, merge.timestamp
WINDOW = timedelta(days=7)
GRACE = timedelta(minutes=5)
WORKFLOW = 'repair-recovery.yml'


class API(merge.API):
    def call(self, path):
        return self.request(path)

    def job_log(self, job):
        class StripCredentials(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, url):
                require(urllib.parse.urlparse(url).scheme == 'https', 'insecure log redirect')
                redirected = super().redirect_request(request, fp, code, message, headers, url)
                redirected.remove_header('Authorization')
                return redirected
        request = urllib.request.Request(self.root + f'/actions/jobs/{job}/logs',
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json'})
        with urllib.request.build_opener(StripCredentials()).open(request, timeout=30) as response:
            content = response.read(8_000_001)
        require(len(content) <= 8_000_000, 'publication log exceeds recovery limit')
        return content.decode('utf-8')


def newest(runs):
    return max(runs, key=lambda r: (timestamp(r.get('run_started_at') or r['created_at']), r['id'], r['run_attempt']))


def classification(runs, jobs, published, now):
    if now - published < GRACE:
        return 'awaiting CI: publication grace period'
    if any(r['status'] != 'completed' for r in runs):
        return 'awaiting CI: full run is active'
    if not runs:
        return 'recover: missing post-repair dispatch'
    current = newest(runs)
    if current['conclusion'] == 'success':
        return 'awaiting Renovate request or checked merge: newest CI succeeded'
    if current['conclusion'] == 'action_required' and current['event'] == 'pull_request' and not jobs:
        dispatched = [r for r in runs if r['event'] == 'workflow_dispatch']
        if len(runs) == 1:
            return 'recover: missing post-repair dispatch'
        # An older deterministic failure cannot become retryable approval noise.
        if (dispatched and newest(dispatched)['conclusion'] == 'success'
                and all(r['conclusion'] in {'success', 'action_required'} for r in runs)):
            return 'recover: approval-required duplicate with zero jobs'
    return 'blocked: newest full CI did not succeed; inspect ' + str(current['html_url'])


def workflow(api, filename):
    result = api.request('/actions/workflows/' + filename)
    require(result['path'] == '.github/workflows/' + filename and result['state'] == 'active', 'workflow identity changed')
    return result


def candidate(api, repository, branch, base, config, number, now):
    options = config['repair_recovery']
    pr = api.request(f'/pulls/{number}')
    head = sha(pr['head']['sha'])
    require(repair.eligible(pr, repository, head), 'not a current same-repository Renovate PR')
    require(pr['base']['ref'] == branch and pr['base']['sha'] == base, 'recovery base changed')
    require(not {'manual-dependencies', 'do-not-merge'} & {label['name'] for label in pr.get('labels', [])}, 'manual dependency policy applies')
    merge.approvals(pr, api.pages(f'/pulls/{number}/reviews'), 0)
    commit = api.request('/commits/' + head)
    author = commit.get('author') or {}
    require(author.get('id') == 41898282 and author.get('login') == 'github-actions[bot]' and author.get('type') == 'Bot', 'head is not a GitHub Actions repair')
    require(commit['commit']['message'] == 'chore(deps): migrate Biome configuration and formatting\n\nSigned-off-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>', 'head is not a recognized repair commit')
    require(len(commit['parents']) == 1, 'repair has unexpected parents')
    parent = sha(commit['parents'][0]['sha'])
    class ParentAPI:
        def call(self, path):
            if path == f'/pulls/{number}':
                return {**pr, 'head': {**pr['head'], 'sha': parent}}
            return api.request(path)
    _, evidence = repair.version_change(ParentAPI(), repository, number, parent, options['package_directory'])
    require(evidence['base'] == base and evidence['old_version'] != evidence['new_version'], 'repair dependency change no longer applies')
    compare = api.request(f'/compare/{parent}...{head}')
    files = compare.get('files', [])
    require(compare['ahead_by'] == 1 and compare['behind_by'] == 0 and 1 <= len(files) <= 200, 'repair diff is incomplete or not one commit')
    tree = api.request('/git/trees/' + commit['commit']['tree']['sha'] + '?recursive=1')
    require(not tree.get('truncated'), 'repair tree is truncated')
    entries = {f['path']: f for f in tree['tree']}
    require(all(f['status'] == 'modified' and repair.allowed(f['filename'], options['config_files'], options['source_roots'])
                and entries.get(f['filename'], {}).get('mode') in {'100644', '100755'}
                and entries[f['filename']]['type'] == 'blob' for f in files), 'repair changed forbidden files')
    source = workflow(api, 'biome-repair.yml')
    runs = api.pages(f"/actions/workflows/{source['id']}/runs?head_sha={parent}", 'workflow_runs')
    expected = {'workflow': config['ci_workflow'], 'ref': pr['head']['ref'], 'pr-number': number, 'expected-head-sha': head}
    proved = None
    for run in sorted(runs, key=lambda r: r['id'], reverse=True):
        if not (run['workflow_id'] == source['id'] and run['head_sha'] == parent and run['head_branch'] == pr['head']['ref']
                and run['event'] == 'pull_request_target' and run['status'] == 'completed'
                and any(p['number'] == number for p in run.get('pull_requests', []))):
            continue
        if now - timestamp(run['created_at']) > WINDOW:
            continue
        details = api.request(f"/actions/runs/{run['id']}")
        refs = details.get('referenced_workflows', [])
        require(any(re.fullmatch(r'edbfi/automation/\.github/workflows/biome-repair\.yml@(?:v1\.1\.3|' + re.escape(options['automation_ref']) + ')', r['path']) for r in refs), 'repair did not use an approved shared workflow')
        jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", 'jobs')
        publishers = [j for j in jobs if j['name'] == 'repair / publish' and j['status'] == 'completed'
                      and j['run_attempt'] == run['run_attempt'] and j['run_id'] == run['id']]
        for job in publishers:
            log = api.job_log(job['id'])
            for line in log.splitlines():
                marker = 'Full CI dispatch target (also usable for manual recovery): '
                if marker in line:
                    try:
                        receipt = json.loads(line.split(marker, 1)[1])
                    except ValueError:
                        continue
                    if receipt == expected:
                        proved = {'run': run['id'], 'attempt': run['run_attempt'], 'published': timestamp(job['completed_at'])}
        if proved:
            break
    require(proved is not None, 'trusted publication evidence is missing or expired')
    ci = workflow(api, config['ci_workflow'])
    runs = api.pages(f"/actions/workflows/{ci['id']}/runs?head_sha={head}", 'workflow_runs')
    require(all(r['workflow_id'] == ci['id'] and r['head_sha'] == head and r['head_branch'] == pr['head']['ref']
                and r['event'] in {'pull_request', 'workflow_dispatch'}
                and (r['event'] != 'pull_request' or any(p['number'] == number for p in r.get('pull_requests', []))) for r in runs), 'unexpected CI run identity')
    current = newest(runs) if runs else None
    jobs = api.pages(f"/actions/runs/{current['id']}/attempts/{current['run_attempt']}/jobs", 'jobs') if current else []
    return {'head': head, 'base': base, 'branch': pr['head']['ref'], 'parent': parent,
            'proof': proved, 'outcome': classification(runs, jobs, proved['published'], now)}


def reservations(runs, number, head):
    return sorted([r for r in runs if r['display_title'] == f'Repair CI #{number} {head}'], key=lambda r: r['id'])


def reconcile(api, repository, branch, base, config, number, now, inputs, run_id):
    first = candidate(api, repository, branch, base, config, number, now)
    print(f"PR #{number}: {first['outcome']}")
    if not first['outcome'].startswith('recover:'):
        return
    control = workflow(api, WORKFLOW)
    records = api.pages(f"/actions/workflows/{control['id']}/runs?event=workflow_dispatch&created=%3E%3D{(now-WINDOW-timedelta(days=1)).date()}", 'workflow_runs')
    require(all(r['workflow_id'] == control['id'] and r['event'] == 'workflow_dispatch' for r in records), 'invalid recovery history')
    used = reservations(records, number, first['head'])
    require(all(r['head_branch'] == branch for r in used), 'recovery reservation used untrusted branch')
    if inputs.get('pr-number'):
        require(inputs.get('expected-head-sha') == first['head'] and inputs.get('expected-base-sha') == base, 'recovery request is stale')
        require(os.environ['GITHUB_RUN_ATTEMPT'] == '1', 'reruns cannot dispatch additional repair CI')
        require(any(r['id'] == run_id for r in used[:2]), 'recovery reservation missing or budget exhausted')
        require(all(r['status'] == 'completed' and now - timestamp(r['updated_at']) >= GRACE for r in used if r['id'] < run_id), 'an earlier reservation remains active or settling')
        current_base = api.request('/git/ref/heads/' + urllib.parse.quote(branch, safe=''))['object']['sha']
        require(current_base == base and candidate(api, repository, branch, base, config, number, now) == first, 'repair evidence changed before dispatch')
        api.request('/actions/workflows/' + config['ci_workflow'] + '/dispatches', 'POST',
                    {'ref': first['branch'], 'inputs': {'pr-number': str(number), 'expected-head-sha': first['head']}})
        print(f"PR #{number}: awaiting CI; recovery dispatch sent for {first['head']}")
    else:
        require(len(used) < 2, 'automatic recovery budget exhausted; inspect approval-required run')
        if any(r['status'] != 'completed' or now - timestamp(r['updated_at']) < GRACE for r in used):
            print(f'PR #{number}: awaiting CI; recovery reservation active or settling')
            return
        api.request('/actions/workflows/' + WORKFLOW + '/dispatches', 'POST',
                    {'ref': branch, 'inputs': {'pr-number': str(number), 'expected-head-sha': first['head'], 'expected-base-sha': base}})
        print(f'PR #{number}: recovery reserved')


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    repository = os.environ['GITHUB_REPOSITORY']
    require(repository.startswith('edbfi/') and event['repository']['full_name'] == repository, 'recovery repository mismatch')
    api = API(repository, os.environ['GH_TOKEN'])
    live = api.request('')
    branch = live['default_branch']
    require(live['id'] == event['repository']['id'] and live['full_name'] == repository and not live['archived'], 'recovery repository identity changed')
    base = api.request('/git/ref/heads/' + urllib.parse.quote(branch, safe=''))['object']['sha']
    require(os.environ['GITHUB_SHA'] == base, 'recovery must execute the current trusted default branch')
    require(os.environ['GITHUB_EVENT_NAME'] in {'schedule', 'workflow_dispatch', 'workflow_run'}, 'unsupported recovery event')
    config = merge.policy(api, base)
    options = config.get('repair_recovery', {})
    if options.get('enabled') is not True:
        print('Repair CI recovery is disabled by the trusted policy.')
        return
    require(re.fullmatch(r'v\d+\.\d+\.\d+', options['automation_ref']), 'invalid recovery automation release')
    require(options['config_files'] and options['source_roots'], 'recovery needs explicit repair paths')
    for path in options['config_files']:
        require(repair.allowed(path, options['config_files'], []), 'invalid recovery config path')
    for path in options['source_roots']:
        repair.relative(path)
    inputs = event.get('inputs') or {}
    if inputs.get('pr-number'):
        require(inputs['pr-number'].isdecimal(), 'invalid recovery PR number')
        sha(inputs.get('expected-head-sha')); sha(inputs.get('expected-base-sha'))
        numbers = [int(inputs['pr-number'])]
    else:
        require(not inputs.get('expected-head-sha') and not inputs.get('expected-base-sha'), 'incomplete recovery request')
        numbers = [p['number'] for p in api.pages('/pulls?state=open') if merge.is_renovate(p['user'], config)]
    for number in numbers:
        try:
            reconcile(api, repository, branch, base, config, number, datetime.now(timezone.utc), inputs, int(os.environ['GITHUB_RUN_ID']))
        except Blocked as error:
            print(f'PR #{number}: blocked: {error}')
            if inputs.get('pr-number'):
                raise


if __name__ == '__main__':
    try:
        main()
    except Blocked as error:
        raise SystemExit('Blocked: ' + str(error)) from None
    except (KeyError, TypeError, ValueError, OSError):
        raise SystemExit('Blocked: recovery evidence is missing, stale or unavailable.') from None
