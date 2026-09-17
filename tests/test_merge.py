import base64
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('merge_helper', ROOT / 'actions/merge/merge.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
HEAD, BASE, MERGED = 'a' * 40, 'b' * 40, 'c' * 40
REPOSITORY = 'edbfi/automation'
TREE = 'd' * 40
AUTHOR = {'name': 'renovate[bot]', 'email': '29139614+renovate[bot]@users.noreply.github.com'}
TRAILER = f"Signed-off-by: {AUTHOR['name']} <{AUTHOR['email']}>"
EARLY, NOW, LATER = '2026-09-01T10:00:00Z', '2026-09-01T10:01:00Z', '2026-09-01T10:02:00Z'
SOON = '2026-09-01T10:01:01Z'
BOT = {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'}


class FakeAPI:
    def __init__(self):
        self.root = 'https://api.github.com/repos/' + REPOSITORY
        self.config = json.loads((ROOT / '.github/merge-policy.json').read_text())
        self.base = BASE
        self.repo = {'id': 10, 'full_name': REPOSITORY, 'default_branch': 'main', 'archived': False, 'allow_auto_merge': False}
        self.pr = {'state': 'open', 'draft': False, 'merged': False, 'mergeable': True, 'number': 1, 'title': 'chore(deps): update example',
                   'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPOSITORY}},
                   'head': {'ref': 'renovate/example', 'sha': HEAD, 'repo': {'full_name': REPOSITORY}},
                   'user': BOT, 'updated_at': NOW, 'requested_reviewers': [], 'requested_teams': [], 'labels': []}
        self.comments_present = True
        self.comment = {'id': 7, 'user': BOT, 'body': '/merge-when-green', 'created_at': NOW,
                        'updated_at': NOW, 'issue_url': self.root + '/issues/1'}
        self.renovate = {'extends': ['github>edbfi/automation//automerge.json#v1.0.0']}
        self.runs = [{'id': 20, 'workflow_id': 30, 'head_sha': HEAD, 'head_branch': 'renovate/example',
                      'event': 'pull_request', 'pull_requests': [{'number': 1}], 'status': 'completed',
                      'conclusion': 'success', 'run_attempt': 1, 'check_suite_id': 40,
                      'created_at': EARLY, 'run_started_at': EARLY, 'updated_at': EARLY}]
        self.jobs, self.checks = [], []
        self.extra_checks = []
        self.helper_runs = []
        self.status = {'sha': HEAD, 'state': 'pending', 'statuses': []}
        for index, name in enumerate(self.config['required_checks']):
            self.add_job(name, index + 50)
        self.commits = [{'sha': HEAD, 'author': BOT, 'commit': {'author': AUTHOR, 'message': 'chore(deps): update example\n\n' + TRAILER, 'tree': {'sha': TREE}}}]
        self.published = {'author': AUTHOR, 'message': 'chore(deps): update example (#1)\n\n' + TRAILER, 'tree': {'sha': TREE}}
        self.reviews = []
        self.permission = 'admin'
        self.writes = []
        self.before_read = None
        self.snapshot_reads = 0
        self.fail_dispatch = False

    def add_job(self, name, check_id, conclusion='success'):
        self.jobs.append({'name': name, 'run_id': 20, 'run_attempt': 1, 'status': 'completed',
                          'conclusion': conclusion, 'check_run_url': self.root + f'/check-runs/{check_id}'})
        self.checks.append({'id': check_id, 'name': name, 'head_sha': HEAD, 'app': {'id': 15368},
                            'check_suite': {'id': 40}, 'status': 'completed', 'conclusion': conclusion})

    def request(self, path, method='GET', data=None):
        if method != 'GET':
            self.writes.append((path, method, data))
            if path == '/pulls/1/merge':
                self.base = MERGED
                return {'merged': True, 'sha': MERGED}
            if self.fail_dispatch and path.endswith('/dispatches'):
                raise helper.Blocked('dispatch failed')
            return None
        if path == '':
            self.snapshot_reads += 1
            if self.before_read:
                self.before_read(self)
            return deepcopy(self.repo)
        if path.startswith('/git/ref/heads/'):
            return {'object': {'sha': self.base}}
        if path.startswith('/contents/'):
            value = self.config if 'merge-policy.json' in path else self.renovate
            return {'encoding': 'base64', 'content': base64.b64encode(json.dumps(value).encode()).decode()}
        if path == '/pulls/1':
            return deepcopy(self.pr)
        if path == '/git/commits/' + MERGED:
            return deepcopy(self.published)
        if path.startswith('/compare/'):
            return {'ahead_by': 1, 'behind_by': 0, 'merge_base_commit': {'sha': self.base}}
        if path == '/actions/workflows/merge.yml':
            return {'id': 31, 'path': '.github/workflows/merge.yml', 'state': 'active'}
        if path == '/commits/' + HEAD + '/status':
            return deepcopy(self.status)
        if path == '/actions/workflows/ci.yml':
            return {'id': 30, 'path': '.github/workflows/ci.yml', 'state': 'active'}
        if path == '/issues/comments/7':
            return deepcopy(self.comment)
        if path.endswith('/permission'):
            return {'permission': self.permission}
        raise AssertionError('unexpected test API read: ' + path)

    def pages(self, path, field=None):
        if path == '/pulls/1/commits':
            return deepcopy(self.commits)
        if path.endswith('/reviews'):
            return deepcopy(self.reviews)
        if path.startswith('/actions/workflows/31/runs'):
            return deepcopy(self.helper_runs)
        if path == '/commits/' + HEAD + '/check-runs?filter=latest':
            return deepcopy(self.checks + self.extra_checks)
        if path.startswith('/actions/workflows/'):
            return deepcopy(self.runs)
        if path.endswith('/jobs'):
            return deepcopy(self.jobs)
        if path.startswith('/check-suites/'):
            return deepcopy(self.checks)
        if path == '/issues/1/comments':
            return [deepcopy(self.comment)] if self.comments_present else []
        if path == '/pulls?state=open&head=edbfi%3Arenovate%2Fexample':
            return [deepcopy(self.pr)]
        raise AssertionError('unexpected pagination: ' + path)


class MergeTests(unittest.TestCase):
    def test_entrypoint_enforces_action_owner_before_api_access(self):
        metadata = (ROOT / 'actions/merge/action.yml').read_text()
        owner = next(line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.strip().startswith('AUTOMATION_OWNER:'))
        self.assertEqual(owner, 'edbfi')
        for repository in [REPOSITORY, 'other-owner/automation']:
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as directory:
                event_path = Path(directory) / 'event.json'
                event_path.write_text(json.dumps({'repository': {'id': 10, 'full_name': repository, 'default_branch': 'main'}}))
                env = {'GITHUB_EVENT_PATH': str(event_path), 'GITHUB_REPOSITORY': repository,
                       'AUTOMATION_OWNER': owner, 'GH_TOKEN': 'test', 'GITHUB_SHA': BASE,
                       'GITHUB_EVENT_NAME': 'push'}
                with patch.dict(os.environ, env, clear=True), patch.object(helper, 'API', return_value=FakeAPI()) as api:
                    if repository == REPOSITORY:
                        helper.main()
                        api.assert_called_once_with(repository, 'test')
                    else:
                        with self.assertRaisesRegex(helper.Blocked, 'automation account mismatch'):
                            helper.main()
                        api.assert_not_called()

    def test_success_merges_expected_sha_and_dispatches_exact_default_commit(self):
        api = FakeAPI()
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes, [('/pulls/1/merge', 'PUT', {'sha': HEAD, 'merge_method': 'squash',
            'commit_title': 'chore(deps): update example (#1)', 'commit_message': TRAILER}),
            ('/actions/workflows/ci.yml/dispatches', 'POST', {'ref': 'main', 'inputs': {'expected-default-sha': MERGED}})])

    def test_unsigned_mismatched_or_non_trailer_signoffs_block(self):
        for message in ['chore(deps): update example', 'chore(deps): update example\n\nSigned-off-by: someone <other@example.com>', TRAILER + '\n\nchore(deps): update example']:
            self.reject(lambda a: a.commits[0]['commit'].update(message=message))
        self.reject(lambda a: a.commits[0].update(author={'login': 'other', 'id': 1, 'type': 'User'}))
        self.reject(lambda a: a.commits[0].update(sha=BASE))
        self.reject(lambda a: a.pr.update(title='not conventional'))

    def test_repair_signoff_is_preserved_without_fabricating_bot_authorship(self):
        api = FakeAPI()
        repair_author = {'name': 'github-actions[bot]', 'email': '41898282+github-actions[bot]@users.noreply.github.com'}
        repair_trailer = f"Signed-off-by: {repair_author['name']} <{repair_author['email']}>"
        api.commits[0]['sha'] = 'e' * 40
        api.commits.append({'sha': HEAD, 'author': {'login': 'github-actions[bot]', 'id': 41898282, 'type': 'Bot'},
            'commit': {'author': repair_author, 'message': 'chore(deps): migrate Biome\n\n' + repair_trailer, 'tree': {'sha': TREE}}})
        api.published['message'] += '\n' + repair_trailer
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes[0][2]['commit_message'], TRAILER + '\n' + repair_trailer)

    def test_published_tree_and_author_are_verified(self):
        for mutate in [lambda a: a.published.update(tree={'sha': BASE}),
                       lambda a: a.published.update(author={'name': 'other', 'email': 'other@example.com'})]:
            api = FakeAPI()
            mutate(api)
            with self.assertRaises(helper.Blocked):
                helper.merge(api, REPOSITORY, 'main', 1, 7)
            self.assertEqual(len(api.writes), 1)

    def reject(self, mutate):
        api = FakeAPI()
        mutate(api)
        with self.assertRaises(helper.Blocked):
            helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes, [])

    def test_every_non_success_mandatory_conclusion_blocks(self):
        for conclusion in ['failure', 'cancelled', 'neutral', 'skipped', 'timed_out', None]:
            with self.subTest(conclusion=conclusion):
                self.reject(lambda a: a.jobs[0].update(conclusion=conclusion))

    def test_missing_pending_duplicate_unexpected_and_spoofed_checks_block(self):
        mutations = [lambda a: a.jobs.pop(), lambda a: a.jobs[0].update(status='queued'),
                     lambda a: a.jobs.append(deepcopy(a.jobs[0])), lambda a: a.add_job('undeclared', 99),
                     lambda a: a.checks[0].update(app={'id': 123}), lambda a: a.checks[0].update(head_sha=BASE),
                     lambda a: a.checks[0].update(check_suite={'id': 99}), lambda a: a.checks.clear(),
                     lambda a: a.jobs[0].update(run_attempt=2), lambda a: a.runs[0].update(workflow_id=99),
                     lambda a: a.runs[0].update(event='workflow_run'), lambda a: a.runs[0].update(pull_requests=[])]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.reject(mutate)

    def test_newest_pending_or_failed_run_overrides_older_success(self):
        for status, conclusion in [('queued', None), ('completed', 'failure')]:
            self.reject(lambda a: a.runs.append({**a.runs[0], 'id': 21, 'created_at': LATER,
                'run_started_at': LATER, 'status': status, 'conclusion': conclusion}))

    def test_wrong_and_stale_requests_block(self):
        mutations = [lambda a: a.comment.update(user={**BOT, 'id': 1}),
                     lambda a: a.comment.update(updated_at=LATER),
                     lambda a: a.comment.update(body='/merge-anything'), lambda a: a.comment.update(issue_url=a.root + '/issues/2'),
                     lambda a: a.pr.update(labels=[{'name': 'manual-dependencies'}]),
                     lambda a: a.pr.update(user={'login': 'person', 'id': 2, 'type': 'User'}),
                     lambda a: a.renovate.update(automerge=False), lambda a: a.renovate.update(extends=[]),
                     lambda a: a.renovate.update(ignoreTests=True)]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.reject(mutate)

    def test_head_base_repo_policy_and_ci_races_never_merge(self):
        mutations = [lambda a: a.pr['head'].update(sha='d' * 40), lambda a: setattr(a, 'base', 'e' * 40),
                     lambda a: a.repo.update(default_branch='develop'), lambda a: a.config.update(enabled=False),
                     lambda a: a.runs[0].update(run_attempt=2)]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                def install(a):
                    a.before_read = lambda client: mutate(client) if client.snapshot_reads == 2 else None
                self.reject(install)

    def test_draft_fork_unknown_mergeability_and_native_automerge_block(self):
        for mutate in [lambda a: a.pr.update(draft=True), lambda a: a.pr.update(mergeable=None),
                       lambda a: a.pr['head'].update(repo={'full_name': 'contributor/example'}),
                       lambda a: a.pr['base'].update(ref='develop'), lambda a: a.repo.update(allow_auto_merge=True)]:
            self.reject(mutate)

    def test_stale_approval_and_outstanding_review_requests_block(self):
        for state, commit in [('APPROVED', BASE), ('CHANGES_REQUESTED', HEAD), ('DISMISSED', HEAD), ('PENDING', HEAD)]:
            def mutate(a):
                a.config['minimum_approvals'] = 1
                a.reviews = [{'id': 1, 'state': state, 'commit_id': commit, 'user': {'id': 80}}]
            self.reject(mutate)
        self.reject(lambda a: a.pr.update(requested_reviewers=[{'id': 80}]))
        self.reject(lambda a: a.pr.update(requested_teams=[{'id': 80}]))

    def test_current_approval_satisfies_policy(self):
        api = FakeAPI()
        api.config['minimum_approvals'] = 1
        api.reviews = [{'id': 1, 'state': 'APPROVED', 'commit_id': HEAD, 'user': {'id': 80}}]
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes[0][1], 'PUT')

    def test_manual_request_requires_authorization_and_both_commits(self):
        api = FakeAPI()
        api.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=f'/merge {HEAD} {BASE}')
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        for permission, body in [('read', f'/merge {HEAD} {BASE}'), ('admin', f'/merge {HEAD}'), ('admin', f'/merge {HEAD} {MERGED}')]:
            def mutate(a):
                a.permission = permission
                a.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=body)
            self.reject(mutate)

    def test_only_declared_optional_reporting_can_skip(self):
        api = FakeAPI()
        api.config['optional_checks'] = ['report']
        api.add_job('report', 99, 'skipped')
        helper.merge(api, REPOSITORY, 'main', 1, 7)

    def test_retained_request_still_blocks_failed_or_pending_repair(self):
        for conclusion in ['failure', 'cancelled', None]:
            api = FakeAPI()
            api.extra_checks = [{'id': 90, 'name': 'repair / compute', 'head_sha': HEAD,
                                 'status': 'completed' if conclusion else 'queued',
                                 'conclusion': conclusion, 'app': {'id': 15368},
                                 'check_suite': {'id': 91}}]
            with self.assertRaisesRegex(helper.Blocked, 'additional dependency check'):
                helper.resume(api, REPOSITORY, 'main', 1)
            self.assertEqual(api.writes, [])

    def test_release_age_and_other_commit_statuses_must_succeed(self):
        for state in ['pending', 'failure', 'error']:
            self.reject(lambda a: a.status.update(state=state, statuses=[{'id': 90, 'context': 'renovate/stability-days', 'state': state}]))
        api = FakeAPI()
        api.status.update(state='success', statuses=[{'id': 90, 'context': 'renovate/stability-days', 'state': 'success'}])
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes[0][0:2], ('/pulls/1/merge', 'PUT'))

    def test_only_verified_helper_suites_are_excluded(self):
        api = FakeAPI()
        api.extra_checks = [{'id': 90, 'name': 'merge', 'head_sha': HEAD, 'status': 'completed',
                             'conclusion': 'failure', 'app': {'id': 15368}, 'check_suite': {'id': 91}}]
        with self.assertRaises(helper.Blocked): helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes, [])
        api.helper_runs = [{'id': 92, 'workflow_id': 31, 'head_sha': HEAD, 'check_suite_id': 91}]
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes[0][0:2], ('/pulls/1/merge', 'PUT'))

    def test_additional_check_race_is_revalidated(self):
        def install(api):
            def mutate(client):
                if client.snapshot_reads == 2:
                    client.status.update(state='pending', statuses=[{'id': 90, 'context': 'renovate/stability-days', 'state': 'pending'}])
            api.before_read = mutate
        self.reject(install)

    def test_bot_handoff_survives_pr_edits_and_later_current_head_ci(self):
        api = FakeAPI()
        api.pr['updated_at'] = LATER
        api.runs[0]['updated_at'] = LATER
        helper.resume(api, REPOSITORY, 'main', 1)
        self.assertEqual(api.writes[0][0:2], ('/pulls/1/merge', 'PUT'))
        self.assertFalse(any(method == 'DELETE' for _, method, _ in api.writes))

    def test_successful_pr_ci_resumes_genuine_handoff(self):
        api = FakeAPI()
        api.runs[0]['updated_at'] = LATER
        helper.complete(api, 'main', deepcopy(api.runs[0]))
        self.assertEqual(api.writes[0][0:2], ('/pulls/1/merge', 'PUT'))

    def test_ci_completion_does_not_create_missing_requests_or_use_stale_events(self):
        for change in ['missing', 'wrong_head', 'older_run', 'failed', 'edited', 'manual_label']:
            with self.subTest(change=change):
                api = FakeAPI()
                event = deepcopy(api.runs[0])
                if change == 'missing': api.comments_present = False
                if change == 'wrong_head': event['head_sha'] = BASE
                if change == 'older_run': event['id'] = 19
                if change == 'failed': event['conclusion'] = 'failure'
                if change == 'edited': api.comment['updated_at'] = LATER
                if change == 'manual_label': api.pr['labels'] = [{'name': 'do-not-merge'}]
                if change in {'older_run', 'edited', 'manual_label'}:
                    with self.assertRaises(helper.Blocked): helper.complete(api, 'main', event)
                else:
                    helper.complete(api, 'main', event)
                self.assertEqual(api.writes, [])

    def test_manual_request_still_requires_current_pr_and_ci_timestamps(self):
        for field in ['pr', 'ci']:
            api = FakeAPI()
            api.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=f'/merge {HEAD} {BASE}')
            if field == 'pr': api.pr['updated_at'] = LATER
            else: api.runs[0]['updated_at'] = LATER
            with self.assertRaises(helper.Blocked): helper.merge(api, REPOSITORY, 'main', 1, 7)
            self.assertEqual(api.writes, [])

    def test_manual_request_tolerates_its_own_pr_timestamp_bump(self):
        api = FakeAPI()
        api.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=f'/merge {HEAD} {BASE}')
        api.pr['updated_at'] = SOON
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes[0][0:2], ('/pulls/1/merge', 'PUT'))

    def test_manual_request_skew_does_not_relax_ci_evidence(self):
        api = FakeAPI()
        api.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=f'/merge {HEAD} {BASE}')
        api.runs[0]['updated_at'] = SOON
        with self.assertRaises(helper.Blocked): helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes, [])

    def test_default_ci_dispatch_failure_is_reported_after_merge(self):
        api = FakeAPI()
        api.fail_dispatch = True
        with patch.object(helper.time, 'sleep'), self.assertRaises(helper.Blocked):
            helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(sum(method == 'PUT' for _, method, _ in api.writes), 1)
        self.assertEqual(sum(path.endswith('/dispatches') for path, _, _ in api.writes), 3)

    def test_deployment_requires_latest_successful_default_branch_ci(self):
        api = FakeAPI()
        api.base = HEAD
        api.config['deploy_workflows'] = ['deploy.yml']
        api.runs[0].update(head_branch='main', event='workflow_dispatch')
        helper.complete(api, 'main', deepcopy(api.runs[0]))
        self.assertEqual(api.writes, [('/actions/workflows/deploy.yml/dispatches', 'POST',
            {'ref': 'main', 'inputs': {'expected-default-sha': HEAD}})])
        api.writes.clear()
        api.runs[0]['conclusion'] = 'failure'
        with self.assertRaises(helper.Blocked):
            helper.complete(api, 'main', deepcopy(api.runs[0]))
        self.assertEqual(api.writes, [])


if __name__ == '__main__':
    unittest.main()


spec = importlib.util.spec_from_file_location('recovery', ROOT / 'actions/ci-recovery/recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = recovery.timestamp('2026-09-17T12:00:00Z')
        self.published = recovery.timestamp('2026-09-16T19:41:24Z')
        self.green = {'id': 35142063850, 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
                      'created_at': '2026-09-16T19:41:24Z', 'run_started_at': '2026-09-16T19:41:24Z', 'run_attempt': 1,
                      'html_url': 'https://github.com/edbfi/zondarr/actions/runs/35142063850'}
        self.duplicate = {**self.green, 'id': 35142069414, 'event': 'pull_request', 'conclusion': 'action_required',
                          'created_at': '2026-09-16T19:41:27Z', 'run_started_at': '2026-09-16T19:41:27Z',
                          'html_url': 'https://github.com/edbfi/zondarr/actions/runs/35142069414'}
        self.candidate = {'head': HEAD, 'base': BASE, 'branch': 'renovate/biome', 'parent': 'd' * 40,
                          'proof': {'run': 1}, 'outcome': 'recover: missing post-repair dispatch'}
        self.inputs = {'pr-number': '206', 'expected-head-sha': HEAD, 'expected-base-sha': BASE}

    def test_observed_206_ordering_requires_new_full_ci(self):
        self.assertEqual(recovery.classification([self.green, self.duplicate], [], self.published, self.now),
                         'recover: approval-required duplicate with zero jobs')
        for conclusion in ['failure', 'cancelled', 'timed_out', 'skipped']:
            self.assertTrue(recovery.classification([self.green, {**self.duplicate, 'conclusion': conclusion}], [], self.published, self.now).startswith('blocked:'))
        self.assertTrue(recovery.classification([self.green, self.duplicate], [{'name': 'guard'}], self.published, self.now).startswith('blocked:'))
        self.assertTrue(recovery.classification([{**self.green, 'conclusion': 'failure'}, self.duplicate], [], self.published, self.now).startswith('blocked:'))

    def test_pending_and_successful_runs_do_not_dispatch(self):
        for status in ['queued', 'in_progress', 'waiting', 'pending']:
            outcome = recovery.classification([self.green, {**self.duplicate, 'status': status}], [], self.published, self.now)
            self.assertTrue(outcome.startswith('awaiting CI:'))
        self.assertFalse(recovery.classification([self.green], [], self.published, self.now).startswith('recover:'))
        self.assertTrue(recovery.classification([], [], self.now, self.now).startswith('awaiting CI:'))
        self.assertTrue(recovery.classification([], [], self.published, self.now).startswith('recover:'))

    def api(self, records):
        class API:
            def __init__(self):
                self.writes = []
            def request(self, path, method='GET', data=None):
                if method == 'POST':
                    self.writes.append((path, data))
                    return None
                if path == '/actions/workflows/repair-recovery.yml':
                    return {'id': 9, 'path': '.github/workflows/repair-recovery.yml', 'state': 'active'}
                if path == '/git/ref/heads/main':
                    return {'object': {'sha': BASE}}
                raise AssertionError(path)
            def pages(self, path, field=None):
                return deepcopy(records)
        return API()

    def record(self, number=1, **changes):
        return {'id': number, 'display_title': f'Repair CI #206 {HEAD}', 'workflow_id': 9, 'event': 'workflow_dispatch',
                'head_branch': 'main', 'status': 'completed', 'updated_at': '2026-09-16T20:00:00Z', **changes}

    def reconcile(self, api, inputs=None, run_id=1):
        with patch.object(recovery, 'candidate', return_value=self.candidate), patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '1'}):
            recovery.reconcile(api, REPOSITORY, 'main', BASE, {'ci_workflow': 'ci.yml'}, 206, self.now, inputs or {}, run_id)

    def test_reservation_precedes_ci_and_duplicate_events_wait(self):
        api = self.api([])
        self.reconcile(api)
        self.assertEqual(api.writes, [('/actions/workflows/repair-recovery.yml/dispatches', {'ref': 'main', 'inputs': self.inputs})])
        for record in [self.record(status='in_progress'), self.record(updated_at='2026-09-17T11:59:00Z')]:
            api = self.api([record])
            self.reconcile(api)
            self.assertFalse(api.writes)

    def test_only_two_recorded_attempts_and_no_reruns(self):
        api = self.api([self.record()])
        self.reconcile(api, self.inputs)
        self.assertEqual(api.writes[0][1], {'ref': 'renovate/biome', 'inputs': {'pr-number': '206', 'expected-head-sha': HEAD}})
        for records, run_id, inputs in [([], 1, self.inputs), ([self.record(), self.record(2)], 3, {}),
                                       ([self.record(), self.record(2), self.record(3)], 3, self.inputs)]:
            api = self.api(records)
            with self.assertRaises(recovery.Blocked):
                self.reconcile(api, inputs, run_id)
            self.assertFalse(api.writes)
        with patch.object(recovery, 'candidate', return_value=self.candidate), patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
            api = self.api([self.record()])
            with self.assertRaises(recovery.Blocked):
                recovery.reconcile(api, REPOSITORY, 'main', BASE, {'ci_workflow': 'ci.yml'}, 206, self.now, self.inputs, 1)
            self.assertFalse(api.writes)

    def test_stale_requests_and_racing_evidence_block(self):
        for key in ['expected-head-sha', 'expected-base-sha']:
            api = self.api([self.record()])
            with self.assertRaises(recovery.Blocked):
                self.reconcile(api, {**self.inputs, key: 'f' * 40})
            self.assertFalse(api.writes)
        api = self.api([self.record()])
        with patch.object(recovery, 'candidate', side_effect=[self.candidate, {**self.candidate, 'head': 'f' * 40}]), patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '1'}):
            with self.assertRaises(recovery.Blocked):
                recovery.reconcile(api, REPOSITORY, 'main', BASE, {'ci_workflow': 'ci.yml'}, 206, self.now, self.inputs, 1)
        self.assertFalse(api.writes)

    def test_uncertain_dispatch_does_not_loop(self):
        api = self.api([self.record()])
        original = api.request
        def request(path, method='GET', data=None):
            result = original(path, method, data)
            if method == 'POST':
                raise OSError('response lost after acceptance')
            return result
        api.request = request
        with self.assertRaises(OSError):
            self.reconcile(api, self.inputs)
        self.assertEqual(len(api.writes), 1)


class RecoveryEvidenceTests(unittest.TestCase):
    def api(self):
        from test_repair import FakeGitHub, HEAD as PARENT, NEW as REPAIRED, BASE as DEFAULT, REPO
        class API:
            def __init__(self):
                self.source = FakeGitHub()
                self.source.pr['head']['sha'] = REPAIRED
                self.commit = {'author': {'id': 41898282, 'login': 'github-actions[bot]', 'type': 'Bot'},
                    'parents': [{'sha': PARENT}], 'commit': {'tree': {'sha': 'tree'},
                    'message': 'chore(deps): migrate Biome configuration and formatting\n\nSigned-off-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>'}}
                self.files = [{'filename': 'biome.json', 'status': 'modified'}]
                self.ref = 'edbfi/automation/.github/workflows/biome-repair.yml@v1.1.3'
                self.created = '2026-09-16T19:41:00Z'
                self.receipt = {'workflow': 'ci.yml', 'ref': 'renovate/biome', 'pr-number': 7, 'expected-head-sha': REPAIRED}
                self.ci = []
            def request(self, path, method='GET', data=None):
                assert method == 'GET', 'candidate must never write'
                if path == '/commits/' + REPAIRED:
                    return deepcopy(self.commit)
                if path == f'/compare/{PARENT}...{REPAIRED}':
                    return {'ahead_by': 1, 'behind_by': 0, 'files': deepcopy(self.files)}
                if path.startswith('/actions/workflows/'):
                    filename = path.rsplit('/', 1)[-1]
                    return {'id': 1 if filename == 'biome-repair.yml' else 2, 'path': '.github/workflows/' + filename, 'state': 'active'}
                if path == '/actions/runs/10':
                    return {'referenced_workflows': [{'path': self.ref}]}
                return self.source.call(path)
            def pages(self, path, field=None):
                if path.endswith('/reviews'):
                    return []
                if path.startswith('/actions/workflows/1/runs'):
                    return [{'id': 10, 'workflow_id': 1, 'head_sha': PARENT, 'head_branch': 'renovate/biome',
                             'event': 'pull_request_target', 'status': 'completed', 'conclusion': 'failure',
                             'pull_requests': [{'number': 7}], 'created_at': self.created, 'run_attempt': 1}]
                if path == '/actions/runs/10/attempts/1/jobs':
                    return [{'id': 11, 'name': 'repair / publish', 'status': 'completed', 'conclusion': 'failure',
                             'run_attempt': 1, 'run_id': 10, 'completed_at': '2026-09-16T19:41:26Z'}]
                if path.startswith('/actions/workflows/2/runs'):
                    return deepcopy(self.ci)
                if path.endswith('/jobs'):
                    return []
                raise AssertionError(path)
            def job_log(self, job):
                assert job == 11
                return '2026-09-16T19:41:24Z Full CI dispatch target (also usable for manual recovery): ' + json.dumps(self.receipt)
        return API(), REPO, DEFAULT

    def candidate(self, api, repo, base):
        return recovery.candidate(api, repo, 'main', base, {'ci_workflow': 'ci.yml', 'repair_recovery': {
            'automation_ref': 'v1.2.0', 'package_directory': '.', 'config_files': ['biome.json'], 'source_roots': ['src']}},
            7, recovery.timestamp('2026-09-17T12:00:00Z'))

    def test_missing_dispatch_after_proved_publication_is_recoverable(self):
        api, repo, base = self.api()
        result = self.candidate(api, repo, base)
        self.assertEqual(result['outcome'], 'recover: missing post-repair dispatch')
        self.assertEqual(result['proof']['run'], 10)

    def test_spoofed_provenance_expired_logs_and_forbidden_files_fail(self):
        for kind in ['author', 'workflow', 'receipt', 'expired', 'forbidden', 'new-file', 'stale']:
            api, repo, base = self.api()
            if kind == 'author': api.commit['author']['id'] = 1
            if kind == 'workflow': api.ref = 'attacker/automation/.github/workflows/biome-repair.yml@v1.1.3'
            if kind == 'receipt': api.receipt['expected-head-sha'] = 'f' * 40
            if kind == 'expired': api.created = '2026-08-01T00:00:00Z'
            if kind == 'forbidden': api.files[0]['filename'] = '.github/workflows/ci.yml'
            if kind == 'new-file': api.files[0]['status'] = 'added'
            if kind == 'stale': api.source.pr['base']['sha'] = 'f' * 40
            with self.subTest(kind=kind), self.assertRaises(recovery.Blocked):
                self.candidate(api, repo, base)
