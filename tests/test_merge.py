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
