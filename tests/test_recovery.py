import base64
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
HEAD, BASE = 'a' * 40, 'b' * 40
REPOSITORY = 'example/app'

spec = importlib.util.spec_from_file_location('recovery', ROOT / 'actions/ci-recovery/recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
support = recovery.support


def policy_file(config):
    return {'encoding': 'base64', 'content': base64.b64encode(json.dumps(config).encode()).decode()}


class RecoverySupportTests(unittest.TestCase):
    def test_api_can_read_and_dispatch_but_cannot_publish_or_merge(self):
        requests = []
        def respond(request, timeout):
            requests.append(request)
            return io.BytesIO(b'{}' if request.method == 'GET' else b'')
        api = support.API(REPOSITORY, 'test-token')
        with patch.object(support.urllib.request, 'urlopen', side_effect=respond):
            self.assertEqual(api.request('/pulls/7'), {})
            data = {'ref': 'renovate/biome', 'inputs': {'pr-number': '7', 'expected-head-sha': HEAD}}
            self.assertIsNone(api.request('/actions/workflows/ci.yml/dispatches', 'POST', data))
            for method, path in [('PUT', '/pulls/7/merge'), ('PATCH', '/git/refs/heads/main'),
                                 ('POST', '/git/commits'), ('POST', '/issues/7/comments'),
                                 ('DELETE', '/git/refs/heads/renovate/biome'),
                                 ('POST', '/actions/workflows/../ci.yml/dispatches'),
                                 ('GET', '//untrusted.example/pulls'),
                                 ('GET', '/../../untrusted/pulls')]:
                with self.subTest(method=method, path=path), self.assertRaises(support.Blocked):
                    api.request(path, method, data)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].full_url,
                         'https://api.github.com/repos/example/app/actions/workflows/ci.yml/dispatches')
        self.assertEqual(json.loads(requests[1].data), data)
        self.assertEqual(requests[1].get_header('Authorization'), 'Bearer test-token')

    def test_policy_is_read_from_the_exact_trusted_commit(self):
        config = {'schema': 1, 'ci_workflow': 'ci.yaml', 'repair_recovery': {'enabled': False}}
        class API:
            def request(self, path):
                self.path = path
                return policy_file(config)
        api = API()
        self.assertEqual(support.policy(api, BASE), config)
        self.assertEqual(api.path, '/contents/.github/repair-policy.json?ref=' + BASE)
        with self.assertRaises(support.Blocked):
            support.policy(api, 'main')

    def test_policy_rejects_invalid_shapes_and_workflow_paths(self):
        valid = {'schema': 1, 'ci_workflow': 'ci.yml', 'repair_recovery': {'enabled': False}}
        invalid = [[], {**valid, 'schema': True}, {**valid, 'schema': 2},
                   {**valid, 'repair_recovery': []}, {**valid, 'ci_workflow': None}]
        invalid.extend({**valid, 'ci_workflow': name} for name in
                       ['../ci.yml', 'ci.yml/dispatches', 'ci.yml?ref=main', 'ci', 'ci.yml\n'])
        class API:
            def request(self, path):
                return policy_file(config)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(support.Blocked):
                support.policy(API(), BASE)
        with patch.object(support.API, 'request', return_value={'encoding': 'utf-8'}), \
                self.assertRaisesRegex(support.Blocked, 'encoding'):
            support.policy(support.API(REPOSITORY, 'test-token'), BASE)

    def test_renovate_identity_requires_login_id_and_type(self):
        identity = {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'}
        self.assertTrue(support.is_renovate(identity))
        for key, value in [('login', 'renovate'), ('id', 1), ('type', 'User')]:
            self.assertFalse(support.is_renovate({**identity, key: value}))
            self.assertFalse(support.is_renovate({k: v for k, v in identity.items() if k != key}))

    def test_review_requests_and_unresolved_objections_block_recovery(self):
        for field in ['requested_reviewers', 'requested_teams']:
            with self.subTest(field=field), self.assertRaises(support.Blocked):
                support.require_no_objections({field: [{'id': 1}]}, [])
        objection = {'id': 1, 'user': {'id': 10}, 'state': 'CHANGES_REQUESTED'}
        for later in [{'id': 2, 'user': {'id': 10}, 'state': 'COMMENTED'},
                      {'id': 2, 'user': {'id': 20}, 'state': 'APPROVED'},
                      {'id': 2, 'user': {'id': 10}, 'state': 'PENDING'}]:
            with self.subTest(later=later), self.assertRaises(support.Blocked):
                support.require_no_objections({}, [later, objection])
        for state in ['APPROVED', 'DISMISSED']:
            support.require_no_objections({}, [{'id': 2, 'user': {'id': 10}, 'state': state}, objection])


class RecoveryEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.event = {'repository': {'full_name': REPOSITORY, 'id': 123}}
        self.live = {**self.event['repository'], 'default_branch': 'trunk', 'archived': False}
        self.config = {'schema': 1, 'ci_workflow': 'ci.yml', 'repair_recovery': {
            'enabled': True, 'automation_ref': 'v2.0.0', 'package_directory': '.',
            'config_files': ['biome.json'], 'source_roots': ['src']}}
        self.env = {'GITHUB_REPOSITORY': REPOSITORY, 'GH_TOKEN': 'test-token', 'GITHUB_SHA': BASE,
                    'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_RUN_ID': '42'}

    def run_main(self):
        fixture = self
        class API:
            def request(self, path):
                if path == '':
                    return deepcopy(fixture.live)
                if path == '/git/ref/heads/trunk':
                    return {'object': {'sha': BASE}}
                if path == '/contents/.github/repair-policy.json?ref=' + BASE:
                    return policy_file(fixture.config)
                raise AssertionError(path)
            def pages(self, path):
                assert path == '/pulls?state=open'
                return [{'number': 7, 'user': {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'}},
                        {'number': 8, 'user': {'login': 'renovate[bot]', 'id': 1, 'type': 'Bot'}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'event.json'
            path.write_text(json.dumps(self.event))
            with patch.dict(os.environ, {**self.env, 'GITHUB_EVENT_PATH': str(path)}, clear=True), \
                    patch.object(recovery, 'API', return_value=API()), \
                    patch.object(recovery, 'reconcile') as reconcile:
                recovery.main()
                return reconcile

    def test_non_edbfi_repository_and_non_main_default_branch_are_supported(self):
        reconcile = self.run_main()
        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args.args[1:6], (REPOSITORY, 'trunk', BASE, self.config, 7))

    def test_disabled_policy_does_not_reconcile(self):
        self.config['repair_recovery'] = {'enabled': False}
        self.run_main().assert_not_called()

    def test_event_and_live_repository_identity_must_match(self):
        for source, key, value in [('event', 'full_name', 'other/app'), ('event', 'id', 456),
                                   ('live', 'full_name', 'other/app'), ('live', 'archived', True)]:
            self.setUp()
            target = self.event['repository'] if source == 'event' else self.live
            target[key] = value
            with self.subTest(source=source, key=key), self.assertRaises(recovery.Blocked):
                self.run_main()

    def test_stale_default_branch_or_untrusted_event_cannot_reconcile(self):
        for key, value in [('GITHUB_SHA', HEAD), ('GITHUB_EVENT_NAME', 'pull_request')]:
            self.setUp()
            self.env[key] = value
            with self.subTest(key=key), self.assertRaises(recovery.Blocked):
                self.run_main()


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

    def candidate(self, api, repo, base, automation_ref='v1.2.0'):
        return recovery.candidate(api, repo, 'main', base, {'ci_workflow': 'ci.yml', 'repair_recovery': {
            'automation_ref': automation_ref, 'package_directory': '.', 'config_files': ['biome.json'], 'source_roots': ['src']}},
            7, recovery.timestamp('2026-09-17T12:00:00Z'))

    def test_missing_dispatch_after_proved_publication_is_recoverable(self):
        api, repo, base = self.api()
        result = self.candidate(api, repo, base)
        self.assertEqual(result['outcome'], 'recover: missing post-repair dispatch')
        self.assertEqual(result['proof']['run'], 10)

    def test_legacy_and_explicitly_configured_releases_preserve_publication_proof(self):
        for configured in ['v1.2.0', 'v2.0.0']:
            for published in ['v1.1.3', configured]:
                api, repo, base = self.api()
                api.ref = 'edbfi/automation/.github/workflows/biome-repair.yml@' + published
                with self.subTest(configured=configured, published=published):
                    result = self.candidate(api, repo, base, configured)
                    self.assertEqual(result['proof']['run'], 10)
        for published in ['v1', 'v2', 'main', 'v2.0.1']:
            api, repo, base = self.api()
            api.ref = 'edbfi/automation/.github/workflows/biome-repair.yml@' + published
            with self.subTest(published=published), self.assertRaisesRegex(recovery.Blocked, 'approved shared workflow'):
                self.candidate(api, repo, base, 'v2.0.0')

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
