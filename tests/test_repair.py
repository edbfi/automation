import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


repair = module('repair', 'actions/biome-repair/repair.py')
guard = module('guard', 'actions/dispatch-guard/guard.py')
BASE = 'c' * 40
HEAD = 'a' * 40
NEW = 'b' * 40
REPO = 'example/app'
PR = {'state': 'open', 'draft': False, 'user': {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'},
      'head': {'sha': HEAD, 'ref': 'renovate/biome', 'repo': {'full_name': REPO, 'id': 123}},
      'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPO, 'id': 123}}}
EVIDENCE = {'base': BASE, 'directory': '.', 'old_version': '2.4.11', 'new_version': '2.5.13', 'repository_id': 123}
PAYLOAD = {'evidence': EVIDENCE, 'head': HEAD, 'pr': 7, 'repository': REPO, 'files': [
    {'path': 'biome.json', 'content': base64.b64encode(b'{"formatter": {"enabled": true}}\n').decode()}]}


class FakeGitHub:
    dispatch = repair.GitHub.dispatch

    def __init__(self, race=False):
        self.calls = []
        self.race = race
        self.pr = copy.deepcopy(PR)
        self.old_version = '2.4.11'
        self.new_version = '2.5.13'
        self.directory = '.'
        self.ref = HEAD

    def call(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if path == '/pulls/7':
            return copy.deepcopy(self.pr)
        if path == '':
            return {'id': 123, 'full_name': REPO, 'default_branch': 'main'}
        if path == '/git/ref/heads/main':
            return {'object': {'sha': self.pr['base']['sha']}}
        if path == '/git/ref/heads/renovate%2Fbiome':
            return {'object': {'sha': self.ref}}
        if path.startswith('/compare/'):
            return {'behind_by': 0, 'merge_base_commit': {'sha': self.pr['base']['sha']}}
        if path.startswith('/contents/'):
            prefix = '' if self.directory == '.' else self.directory + '/'
            file, ref = path.removeprefix('/contents/').split('?ref=')
            assert file in {prefix + 'package.json', prefix + 'bun.lock'}
            version = self.old_version if ref == BASE else self.new_version
            manifest = {'devDependencies': {'@biomejs/biome': '^2.4.11'}}
            value = manifest if file.endswith('package.json') else {
                'workspaces': {'': manifest},
                'packages': {'@biomejs/biome': ['@biomejs/biome@' + version, '', {}, 'sha512-fixture']}}
            return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(json.dumps(value).encode()).decode()}
        if path == f'/git/commits/{HEAD}':
            return {'tree': {'sha': 'tree'}}
        if path == '/git/trees/tree?recursive=1':
            return {'tree': [{'path': 'biome.json', 'type': 'blob', 'mode': '100644'}]}
        if path == '/git/blobs':
            return {'sha': 'blob'}
        if path == '/git/trees':
            return {'sha': 'new-tree'}
        if path == '/git/commits':
            assert data['parents'] == [HEAD]
            return {'sha': NEW}
        if path.startswith('/git/refs/'):
            assert data == {'sha': NEW, 'force': False}
            if self.race:
                raise RuntimeError('non-fast-forward')
            self.pr['head']['sha'] = NEW
            self.ref = NEW
            return {}
        if path == '/actions/workflows/ci.yml/dispatches':
            assert data == {'ref': 'renovate/biome', 'inputs': {'pr-number': '7', 'expected-head-sha': NEW}}
            return None
        raise AssertionError(path)


class RepairTests(unittest.TestCase):
    def test_only_branch_update_receives_app_credential(self):
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            return io.BytesIO(b'{}')
        with patch.dict(os.environ, {'GH_TOKEN': 'workflow-test', 'PUBLISH_TOKEN': 'app-test'}, clear=True), \
                patch.object(repair.urllib.request, 'urlopen', side_effect=open_request):
            api = repair.GitHub(REPO)
            api.call('/pulls/7')
            api.call('/git/commits', {'parents': [HEAD]})
            api.call('/git/refs/heads/renovate/biome', {'sha': NEW, 'force': False}, 'PATCH')
            api.dispatch('ci.yml', 'renovate/biome', 7, NEW)
        self.assertEqual([r.get_header('Authorization') for r in requests],
                         ['Bearer workflow-test', 'Bearer workflow-test', 'Bearer app-test', 'Bearer workflow-test'])

    def test_branch_update_without_app_token_fails_before_network(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'workflow-test'}, clear=True), \
                patch.object(repair.urllib.request, 'urlopen') as network, self.assertRaisesRegex(ValueError, 'GitHub App token'):
            repair.GitHub(REPO).call('/git/refs/heads/renovate/biome', {'sha': NEW, 'force': False}, 'PATCH')
        network.assert_not_called()

    def test_pr_head_propagation_retries_reads_without_republishing(self):
        for stale_reads in [2, 10]:
            api = FakeGitHub()
            original = api.call
            reads = 0
            def call(path, data=None, method=None):
                nonlocal reads
                result = original(path, data, method)
                if path == '/pulls/7' and api.ref == NEW:
                    reads += 1
                    if reads <= stale_reads:
                        result['head']['sha'] = HEAD
                return result
            api.call = call
            with patch.object(repair.time, 'sleep') as sleep:
                if stale_reads == 2:
                    self.assertEqual(repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD), NEW)
                    self.assertEqual(sleep.call_count, 2)
                else:
                    with self.assertRaisesRegex(ValueError, 'not visible'):
                        repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
                    self.assertEqual(sleep.call_count, 4)
                    self.assertFalse(any('/dispatches' in p for p, _, _ in api.calls))
            self.assertEqual(sum(method == 'PATCH' for _, _, method in api.calls), 1)

    def test_racing_push_during_head_propagation_fails_without_retry(self):
        for changed in ['ref', 'pr']:
            api = FakeGitHub()
            original = api.call
            def call(path, data=None, method=None):
                result = original(path, data, method)
                if method == 'PATCH':
                    if changed == 'ref':
                        api.ref = 'd' * 40
                    else:
                        api.pr['head']['sha'] = 'd' * 40
                return result
            api.call = call
            with patch.object(repair.time, 'sleep') as sleep, self.assertRaisesRegex(ValueError, 'changed after publication'):
                repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
            sleep.assert_not_called()
            self.assertFalse(any('/dispatches' in p for p, _, _ in api.calls))

    def test_uncertain_dispatch_is_not_blindly_retried(self):
        api = FakeGitHub()
        original = api.call
        count = 0
        def call(path, data=None, method=None):
            nonlocal count
            if path.endswith('/dispatches'):
                count += 1
                raise repair.urllib.error.URLError('accepted but response lost')
            return original(path, data, method)
        api.call = call
        with self.assertRaises(repair.urllib.error.URLError):
            repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
        self.assertEqual(count, 1)
        self.assertEqual(api.pr['head']['sha'], NEW)

    def test_schema_only_update_does_not_run_tools_or_publish_a_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'package.json').write_text(json.dumps({'devDependencies': {'@biomejs/biome': '^2.4.11'}}))
            config = '{"$schema":"https://biomejs.dev/schemas/2.5.12/schema.json"}\n'
            (root / 'biome.json').write_text(config)
            api = FakeGitHub()
            api.new_version = api.old_version
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO, 'GITHUB_OUTPUT': str(root / 'outputs')}), \
                    patch.object(repair.subprocess, 'run') as run:
                repair.compute(['biome.json'], ['src'], '.', api, 7, HEAD, root / 'repair.json')
            run.assert_not_called()
            self.assertEqual((root / 'outputs').read_text(), 'changed=false\n')
            self.assertFalse((root / 'repair.json').exists())
            self.assertEqual((root / 'biome.json').read_text(), config)

    def test_real_biome_migrates_and_formats_idempotently(self):
        version = json.loads((Path(__file__).parents[1] / 'package.json').read_text())['devDependencies']['@biomejs/biome']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'src').mkdir()
            (root / 'frontend/src').mkdir(parents=True)
            (root / 'package.json').write_text(json.dumps({'devDependencies': {'@biomejs/biome': version}}))
            for file, config in [('biome.json', {}), ('frontend/biome.json', {'root': False})]:
                (root / file).write_text(json.dumps({'$schema': 'https://biomejs.dev/schemas/2.4.0/schema.json', **config}))
            for file in ['src/example.ts', 'frontend/src/example.ts']:
                (root / file).write_text('export const value={name:"example"}\n')
            def run(*args):
                return subprocess.check_output(args, cwd=root, stderr=subprocess.STDOUT).decode().strip()
            run('bun', 'install', '--ignore-scripts')
            shutil.rmtree(root / 'node_modules')
            run('git', 'init', '-q')
            run('git', 'add', 'package.json', 'bun.lock', 'biome.json', 'frontend', 'src')
            run('git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'test: initialize fixture')
            head = run('git', 'rev-parse', 'HEAD')
            class API(FakeGitHub):
                def call(self, path, data=None, method=None):
                    if path.startswith('/contents/') and path.endswith('?ref=' + head):
                        file = path.removeprefix('/contents/').split('?ref=')[0]
                        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode((root / file).read_bytes()).decode()}
                    return super().call(path, data, method)
            api = API()
            api.pr['head']['sha'] = head
            tool_calls = []
            run_process = subprocess.run
            credentials = {'GH_TOKEN': 'workflow-test', 'PUBLISH_TOKEN': 'publisher-test',
                           'GITHUB_TOKEN': 'github-test', 'NODE_AUTH_TOKEN': 'registry-test'}
            def run_without_credentials(command, *args, **kwargs):
                if command[0] == 'bun' or Path(command[0]).name == 'biome':
                    tool_calls.append(command)
                    self.assertFalse(set(credentials) & kwargs['env'].keys())
                    if command[0] == 'bun':
                        self.assertNotEqual(Path(kwargs['cwd']).resolve(), root.resolve())
                return run_process(command, *args, **kwargs)
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {**credentials, 'GITHUB_REPOSITORY': REPO, 'GITHUB_OUTPUT': str(root / 'outputs')}), \
                        patch.object(repair.subprocess, 'run', side_effect=run_without_credentials):
                    repair.compute(['biome.json', 'frontend/biome.json'], ['src', 'frontend/src'], '.', api, 7, head, root / 'repair.json')
                self.assertFalse((root / 'node_modules').exists())
                self.assertTrue(any(command[0] == 'bun' for command in tool_calls))
                self.assertTrue(any(Path(command[0]).name == 'biome' for command in tool_calls))
                payload = json.loads((root / 'repair.json').read_text())
                self.assertEqual({f['path'] for f in payload['files']}, {'biome.json', 'frontend/biome.json', 'src/example.ts', 'frontend/src/example.ts'})
                for file in ['biome.json', 'frontend/biome.json']:
                    self.assertIn(f'/schemas/{version}/schema.json', (root / file).read_text())
                run('git', 'add', 'biome.json', 'frontend/biome.json', 'src', 'frontend/src')
                run('git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'chore(deps): repair fixture')
                head = run('git', 'rev-parse', 'HEAD')
                api.pr['head']['sha'] = head
                (root / 'repair.json').unlink()
                (root / 'outputs').write_text('')
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO, 'GITHUB_OUTPUT': str(root / 'outputs')}):
                    repair.compute(['biome.json', 'frontend/biome.json'], ['src', 'frontend/src'], '.', api, 7, head, root / 'repair.json')
                self.assertEqual((root / 'outputs').read_text(), 'changed=false\n')
                self.assertFalse((root / 'repair.json').exists())
            finally:
                os.chdir(previous)

    def test_wrong_isolated_tool_identity_stops_before_formatter_execution(self):
        for metadata in [{'name': 'unofficial-biome', 'version': '2.5.13'},
                         {'name': '@biomejs/biome', 'version': '2.5.12'}]:
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def install(command, **kwargs):
                    package = Path(kwargs['cwd']) / 'node_modules/@biomejs/biome/package.json'
                    package.parent.mkdir(parents=True)
                    package.write_text(json.dumps(metadata))
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO, 'GITHUB_OUTPUT': str(root / 'outputs')}), \
                        patch.object(repair.subprocess, 'run', side_effect=install) as run, \
                        patch.object(repair.subprocess, 'check_output') as git, \
                        self.assertRaisesRegex(ValueError, 'official stable Biome'):
                    repair.compute(['biome.json'], ['src'], '.', FakeGitHub(), 7, HEAD, root / 'repair.json')
                self.assertEqual(run.call_count, 1)
                git.assert_not_called()
                self.assertFalse((root / 'repair.json').exists())

    def test_only_current_same_repository_renovate_pr_is_eligible(self):
        self.assertTrue(repair.eligible(PR, REPO, HEAD))
        for change in [{'state': 'closed'}, {'draft': True}, {'user': {'login': 'someone'}},
                       {'user': {**PR['user'], 'id': 1}},
                       {'user': {**PR['user'], 'type': 'User'}},
                       {'user': {'login': 'renovate[bot]'}},
                       {'head': {'sha': HEAD, 'repo': {'full_name': 'someone/fork'}}}]:
            self.assertFalse(repair.eligible({**PR, **change}, REPO, HEAD))
        self.assertFalse(repair.eligible(PR, REPO, NEW))

    def test_protected_and_escaping_paths_are_rejected(self):
        for path in ['../biome.json', '/biome.json', '.github/workflows/ci.yml', 'src/../package.json',
                     'src/package.json', 'renovate.json', 'src/file.sh', '.git/config', 'src/.hidden.ts']:
            with self.subTest(path=path):
                self.assertFalse(repair.allowed(path, ['biome.json'], ['src']))
        self.assertTrue(repair.allowed('frontend/biome.json', ['frontend/biome.json'], []))
        self.assertTrue(repair.allowed('src/lib/file.ts', ['biome.json'], ['src']))

    def test_payload_rejects_duplicate_empty_and_binary_output(self):
        for files in [[], PAYLOAD['files'] * 2, [{'path': 'biome.json', 'content': 'AA=='}]]:
            with self.assertRaises(ValueError):
                repair.validate_payload({**PAYLOAD, 'files': files}, ['biome.json'], ['src'])

    def test_publish_dispatches_the_new_commit(self):
        api = FakeGitHub()
        self.assertEqual(repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD), NEW)
        self.assertEqual(api.calls[-1][0], '/actions/workflows/ci.yml/dispatches')

    def test_concurrent_push_cannot_be_overwritten(self):
        api = FakeGitHub(race=True)
        with self.assertRaises(RuntimeError):
            repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
        self.assertFalse(any('/dispatches' in path for path, _, _ in api.calls))

    def test_base_change_after_publication_prevents_dispatch(self):
        api = FakeGitHub()
        original = api.call
        def call(path, data=None, method=None):
            result = original(path, data, method)
            if method == 'PATCH':
                api.pr['base']['sha'] = 'd' * 40
            return result
        api.call = call
        with self.assertRaisesRegex(ValueError, 'base changed after publication'):
            repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
        self.assertEqual(api.pr['head']['sha'], NEW)
        self.assertFalse(any('/dispatches' in path for path, _, _ in api.calls))

    def test_wrong_artifact_is_rejected_before_writes(self):
        api = FakeGitHub()
        with self.assertRaises(ValueError):
            repair.publish({**PAYLOAD, 'head': NEW}, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
        self.assertEqual(api.calls, [])

    def test_dispatch_must_match_run_sha_branch_and_current_pr(self):
        self.assertTrue(guard.validate(PR, REPO, HEAD, 'renovate/biome', HEAD))
        self.assertFalse(guard.validate(PR, REPO, NEW, 'renovate/biome', HEAD))
        self.assertFalse(guard.validate(PR, REPO, HEAD, 'main', HEAD))
        self.assertFalse(guard.validate({**PR, 'state': 'closed'}, REPO, HEAD, 'renovate/biome', HEAD))


class VersionDetectionTests(unittest.TestCase):
    def test_routes_and_nested_directories_use_only_the_selected_lock(self):
        for branch in ['renovate/biome', 'renovate/biome-2.x', 'renovate/lock-file-maintenance']:
            for directory in ['.', 'frontend', 'packages/ui']:
                api = FakeGitHub()
                api.directory = directory
                api.pr['head']['ref'] = branch
                _, evidence = repair.version_change(api, REPO, 7, HEAD, directory)
                self.assertEqual(evidence['new_version'], '2.5.13')
                self.assertEqual(evidence['directory'], directory)
        for branch in ['renovate/biome-spoof/path', 'renovate/biomeevil', 'renovate/lock-file-maintenance-spoof', 'human']:
            api = FakeGitHub()
            api.pr['head']['ref'] = branch
            with self.assertRaises(ValueError):
                repair.version_change(api, REPO, 7, HEAD, '.')

    def test_unchanged_version_is_detected_even_when_lock_or_manifest_changes(self):
        api = FakeGitHub()
        api.new_version = api.old_version
        _, evidence = repair.version_change(api, REPO, 7, HEAD, '.')
        self.assertEqual(evidence['old_version'], evidence['new_version'])

    def test_stale_base_and_repository_identity_fail(self):
        for mutation in ['behind', 'repository', 'base']:
            api = FakeGitHub()
            original = api.call
            def call(path, data=None, method=None):
                result = original(path, data, method)
                if mutation == 'behind' and path.startswith('/compare/'):
                    result['behind_by'] = 1
                if mutation == 'repository' and path == '':
                    result['id'] = 456
                if mutation == 'base' and path == '/git/ref/heads/main':
                    result['object']['sha'] = NEW
                return result
            api.call = call
            with self.assertRaises(ValueError):
                repair.version_change(api, REPO, 7, HEAD, '.')

    def test_publication_rechecks_version_directory_and_base_before_writes(self):
        for change in [{'new_version': '2.5.14'}, {'directory': 'frontend'}, {'base': NEW}]:
            api = FakeGitHub()
            payload = {**PAYLOAD, 'evidence': {**EVIDENCE, **change}}
            with self.assertRaises(ValueError):
                repair.publish(payload, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
            self.assertFalse(any(data is not None for _, data, _ in api.calls))

    def test_jsonc_preserves_strings_and_rejects_duplicate_keys(self):
        self.assertEqual(repair.jsonc('{"url":"https://example.test/a,}",// comment\n"a":[1,],}'),
                         {'url': 'https://example.test/a,}', 'a': [1]})
        with self.assertRaises(ValueError):
            repair.jsonc('{"packages":{},"packages":{}}')

    def test_ambiguous_workspace_and_unofficial_resolution_fail(self):
        manifest = {'devDependencies': {'@biomejs/biome': '^2.4.11'}}
        valid = {'workspaces': {'': manifest}, 'packages': {'@biomejs/biome': ['@biomejs/biome@2.5.13', '', {}, 'sha512-fixture']}}
        for mutate in [lambda lock: lock['workspaces'].update({'nested': manifest}),
                       lambda lock: lock['packages']['@biomejs/biome'].__setitem__(0, 'file:tool'),
                       lambda lock: lock['packages']['@biomejs/biome'].__setitem__(1, 'https://untrusted.example'),
                       lambda lock: lock['packages'].update({'nested/@biomejs/biome': []})]:
            lock = copy.deepcopy(valid)
            mutate(lock)
            with self.assertRaises(ValueError):
                repair.locked_version(manifest, lock)
