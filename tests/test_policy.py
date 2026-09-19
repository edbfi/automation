from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('policy', Path(__file__).parents[1] / 'actions/pr-policy/policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
HEAD = 'a' * 40
BOT = {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'}


def fixture():
    pr = {'state': 'open', 'draft': False, 'title': 'chore(deps): update dependency',
          'head': {'sha': HEAD, 'ref': 'renovate/example', 'repo': {'full_name': 'example/app'}}, 'base': {'sha': 'b' * 40, 'repo': {'full_name': 'example/app'}},
          'labels': [], 'requested_reviewers': [], 'requested_teams': [], 'commits': 1, 'user': BOT}
    commits = [{'sha': HEAD, 'author': BOT, 'commit': {'author': {'name': 'Renovate Bot', 'email': 'bot@example.com'},
                'message': 'chore(deps): update dependency\n\nSigned-off-by: Renovate Bot <bot@example.com>'}}]
    return pr, commits


def validate(pr, reviews, commits, minimum=0):
    return policy.validate(pr, reviews, commits, 'example/app', HEAD, minimum)


def review(state, identity=2, index=1, head=HEAD):
    return {'id': index, 'state': state, 'user': {'id': identity}, 'commit_id': head}


class PolicyTests(unittest.TestCase):
    def test_valid_current_dependency_and_human_prs_pass(self):
        pr, commits = fixture()
        validate(pr, [], commits)
        pr['user'] = {'login': 'human', 'id': 7, 'type': 'User'}
        commits[0]['author'] = pr['user']
        validate(pr, [], commits)

    def test_noncurrent_closed_draft_and_wrong_repository_fail(self):
        pr, commits = fixture()
        variants = [{**pr, 'state': 'closed'}, {**pr, 'draft': True}, {**pr, 'head': {'sha': 'c' * 40}},
                    {**pr, 'base': {'repo': {'full_name': 'other/app'}}}]
        for item in variants:
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate(item, [], commits)

    def test_titles_and_hold_labels_block_all_authors(self):
        pr, commits = fixture()
        for title in ['update dependency', 'feat: x\nfix: y', 'feat: ']:
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, 'Conventional'):
                validate({**pr, 'title': title}, [], commits)
        validate({**pr, 'title': 'feat(api)!: change contract'}, [], commits)
        for author in [BOT, {'id': 3}]:
            for label in policy.HOLDS:
                with self.subTest(label=label, author=author), self.assertRaisesRegex(ValueError, 'hold'):
                    validate({**pr, 'user': author, 'labels': [{'name': label}]}, [], commits)

    def test_review_requests_pending_objections_and_unknown_states_block(self):
        pr, commits = fixture()
        for field in ['requested_reviewers', 'requested_teams']:
            with self.assertRaisesRegex(ValueError, 'outstanding'):
                validate({**pr, field: [{'id': 1}]}, [], commits)
        for state in ['PENDING', 'CHANGES_REQUESTED', 'unknown']:
            with self.subTest(state=state), self.assertRaises(ValueError):
                validate(pr, [review(state)], commits)
        validate(pr, [review('CHANGES_REQUESTED'), review('DISMISSED', index=2)], commits)
        with self.assertRaisesRegex(ValueError, 'changes'):
            validate(pr, [review('CHANGES_REQUESTED'), review('COMMENTED', index=2)], commits)

    def test_approvals_must_be_latest_current_head_and_not_author(self):
        pr, commits = fixture()
        validate(pr, [review('APPROVED')], commits, 1)
        for reviews in [[review('APPROVED', head='b' * 40)], [review('APPROVED', identity=BOT['id'])],
                        [review('APPROVED'), review('DISMISSED', index=2)], []]:
            with self.subTest(reviews=reviews), self.assertRaisesRegex(ValueError, 'approvals'):
                validate(pr, reviews, commits, 1)

    def test_every_commit_needs_exact_author_trailer_in_final_paragraph(self):
        pr, commits = fixture()
        for message in ['fix: x', 'fix: x\n\nSigned-off-by: Somebody Else <bot@example.com>',
                        'Signed-off-by: Renovate Bot <bot@example.com>\n\nOther text']:
            item = deepcopy(commits)
            item[0]['commit']['message'] = message
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, 'DCO'):
                validate(pr, [], item)
        for name in ['', 'Injected\nAuthor', 'Name <spoof>']:
            item = deepcopy(commits)
            item[0]['commit']['author']['name'] = name
            with self.assertRaisesRegex(ValueError, 'author'):
                validate(pr, [], item)
        item = deepcopy(commits)
        item.insert(0, deepcopy(item[0]))
        item[0]['commit']['message'] = 'unsigned older commit'
        with self.assertRaisesRegex(ValueError, 'DCO'):
            validate({**pr, 'commits': 2}, [], item)

    def test_incomplete_commits_stale_head_and_spoofed_bot_fail(self):
        pr, commits = fixture()
        for current, items in [({**pr, 'commits': 251}, commits), (pr, []), (pr, [{**commits[0], 'sha': 'c' * 40}])]:
            with self.assertRaisesRegex(ValueError, 'complete'):
                validate(current, [], items)
        for author in [None, {**BOT, 'id': 123}, {**BOT, 'type': 'User'}]:
            with self.assertRaisesRegex(ValueError, 'genuine'):
                validate(pr, [], [{**commits[0], 'author': author}])

    def test_renovate_requires_same_repository_and_branch_provenance(self):
        pr, commits = fixture()
        for head in [{**pr['head'], 'ref': 'human/example'},
                     {**pr['head'], 'repo': {'full_name': 'other/app'}}]:
            with self.subTest(head=head), self.assertRaisesRegex(ValueError, 'same-repository'):
                validate({**pr, 'head': head}, [], commits)
        human = {**pr, 'head': head, 'user': {'id': 7, 'type': 'User', 'login': 'contributor'}}
        validate(human, [], commits)

    def test_metadata_and_review_changes_during_evaluation_block(self):
        pr, commits = fixture()
        class API:
            def read(self, path):
                return deepcopy(pr)
            def pages(self, path):
                return deepcopy(commits) if path.endswith('/commits') else []
        policy.evaluate(API(), 7, 'example/app', HEAD)
        for field, value in [('labels', [{'name': 'do-not-merge'}]), ('head', {'sha': 'c' * 40}),
                             ('base', {'sha': 'c' * 40}), ('requested_teams', [{'id': 1}])]:
            with self.subTest(field=field), patch.object(API, 'read', side_effect=[pr, {**pr, field: value}]), \
                    self.assertRaisesRegex(ValueError, 'metadata changed'):
                policy.evaluate(API(), 7, 'example/app', HEAD)
        with patch.object(API, 'pages', side_effect=[[], commits, [review('CHANGES_REQUESTED')]]), \
                self.assertRaisesRegex(ValueError, 'reviews changed'):
            policy.evaluate(API(), 7, 'example/app', HEAD)

    def test_api_has_only_fixed_get_endpoints_and_paginates(self):
        requests = []
        def respond(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps([{}] * 100 if request.full_url.endswith('&page=1') else []).encode())
        api = policy.API('example/app', 'test-token')
        with patch.object(policy.urllib.request, 'urlopen', side_effect=respond):
            self.assertEqual(len(api.pages('/pulls/7/reviews')), 100)
            for path in ['/pulls/7/merge', '/git/refs/heads/main', '/issues/7/comments',
                         '//untrusted.example', '/pulls/7/../8', '/pulls/7?x=1']:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    api.read(path)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.get_method() == 'GET' and request.data is None for request in requests))
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer test-token')


if __name__ == '__main__':
    unittest.main()
