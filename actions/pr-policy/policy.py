"""Read PR metadata and reject policy violations; never mutate GitHub state."""

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

TITLE = re.compile(r'(feat|fix|chore|docs|test|refactor|perf|build|ci|style|revert)(\([^\r\n)]*\))?!?: [^\r\n]+')
HOLDS = {'manual-dependencies', 'do-not-merge'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


class API:
    def __init__(self, repository, token):
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'invalid repository')
        self.root = 'https://api.github.com/repos/' + repository
        self.token = token

    def read(self, path):
        require(re.fullmatch(r'/pulls/[1-9][0-9]*(?:/(?:commits|reviews)\?per_page=100&page=[1-9][0-9]*)?', path),
                'policy may only read pull requests, commits and reviews')
        request = urllib.request.Request(self.root + path, method='GET', headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ValueError(f'GitHub policy evidence returned HTTP {error.code}') from None

    def pages(self, path):
        result = []
        for page in range(1, 101):
            values = self.read(f'{path}?per_page=100&page={page}')
            require(isinstance(values, list), 'invalid paginated policy evidence')
            result.extend(values)
            if len(values) < 100:
                return result
        raise ValueError('policy evidence pagination limit reached')


def validate(pr, reviews, commits, repository, expected_head, minimum_approvals=0):
    require(re.fullmatch(r'[0-9a-f]{40}', expected_head), 'invalid expected PR head')
    require(pr['state'] == 'open' and not pr['draft'], 'PR is closed or draft')
    require(pr['base']['repo']['full_name'] == repository and pr['head']['sha'] == expected_head,
            'PR repository or current head differs from this event')
    require(TITLE.fullmatch(pr['title']), 'PR title is not a Conventional Commit')
    require(not HOLDS & {label['name'] for label in pr['labels']}, 'a merge hold applies')
    require(not pr['requested_reviewers'] and not pr['requested_teams'], 'review requests remain outstanding')
    latest = {}
    for review in sorted(reviews, key=lambda item: item['id']):
        state = review['state']
        require(state in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED', 'COMMENTED', 'PENDING'}, 'unknown review state')
        require(state != 'PENDING', 'a review remains pending')
        if state in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'}:
            latest[review['user']['id']] = review
    require(not any(review['state'] == 'CHANGES_REQUESTED' for review in latest.values()), 'changes are requested')
    require(type(minimum_approvals) is int and minimum_approvals >= 0, 'invalid minimum approvals')
    count = sum(review['state'] == 'APPROVED' and review.get('commit_id') == expected_head
                and review['user']['id'] != pr['user']['id'] for review in latest.values())
    require(count >= minimum_approvals, 'current-head approvals are missing')
    # The pulls/commits endpoint caps its result at 250. Refuse an incomplete list.
    require(commits and len(commits) == pr['commits'] and commits[-1]['sha'] == expected_head,
            'complete PR commit evidence differs from the current head')
    renovate = (pr['user'].get('login') == 'renovate[bot]' and pr['user'].get('id') == 29139614
                and pr['user'].get('type') == 'Bot')
    if renovate:
        require(pr['head']['repo']['full_name'] == repository and pr['head']['ref'].startswith('renovate/'),
                'Renovate PR must use a same-repository renovate/ branch')
    genuine_renovate_commit = False
    for commit in commits:
        author = commit['commit']['author']
        require(all(isinstance(author.get(key), str) and author[key] and not any(c in author[key] for c in '\r\n<>')
                    for key in ('name', 'email')), 'invalid commit author')
        trailer = f"Signed-off-by: {author['name']} <{author['email']}>"
        require(trailer in commit['commit']['message'].rstrip().split('\n\n')[-1].splitlines(),
                'commit is missing its author-matching DCO trailer')
        identity = commit.get('author') or {}
        genuine_renovate_commit |= (identity.get('login') == 'renovate[bot]' and identity.get('id') == 29139614
                                   and identity.get('type') == 'Bot')
    require(not renovate or genuine_renovate_commit, 'PR has no genuine Renovate-authored commit')


def evaluate(api, number, repository, expected_head, minimum_approvals=0):
    path = f'/pulls/{number}'
    first = api.read(path)
    reviews = api.pages(path + '/reviews')
    commits = api.pages(path + '/commits')
    validate(first, reviews, commits, repository, expected_head, minimum_approvals)
    # Re-read mutable policy immediately before concluding. A later metadata event
    # cancels/replaces this job; GitHub does not atomically bind labels to merges.
    current_reviews = api.pages(path + '/reviews')
    current = api.read(path)
    require(current_reviews == reviews, 'reviews changed during policy evaluation')
    fields = ('state', 'draft', 'title', 'labels', 'requested_reviewers', 'requested_teams', 'head', 'base', 'commits')
    require(all(current[field] == first[field] for field in fields), 'PR metadata changed during policy evaluation')
    validate(current, current_reviews, commits, repository, expected_head, minimum_approvals)


def main():
    try:
        require(os.environ['GITHUB_EVENT_NAME'] in {'pull_request', 'pull_request_review'}, 'unsupported policy event')
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        repository = os.environ['GITHUB_REPOSITORY']
        require(event['repository']['full_name'] == repository, 'event repository differs')
        pr = event['pull_request']
        number = pr['number']
        require(type(number) is int and number > 0, 'invalid PR number')
        evaluate(API(repository, os.environ['GH_TOKEN']), number, repository, pr['head']['sha'],
                 int(os.environ.get('MINIMUM_APPROVALS', '0')))
        print('PR title, DCO, holds and review policy passed for ' + pr['head']['sha'])
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise SystemExit('PR policy blocked: ' + str(error)) from None


if __name__ == '__main__':
    main()
