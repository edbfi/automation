"""GitHub reads and bounded workflow dispatches for repair recovery."""
import base64
from datetime import datetime
import json
import re
import urllib.error
import urllib.request


class Blocked(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "invalid commit identity")
    return value


def timestamp(value):
    require(isinstance(value, str), "missing evidence timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class API:
    def __init__(self, repository, token):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "invalid repository")
        self.root = "https://api.github.com/repos/" + repository
        self.token = token

    def request(self, path, method="GET", data=None):
        require(method == 'GET' or (method == 'POST' and re.fullmatch(
            r'/actions/workflows/[A-Za-z0-9_.-]+\.ya?ml/dispatches', path)),
            'recovery may only read evidence or dispatch workflows')
        require(path == "" or (path.startswith("/") and not path.startswith("//") and "/../" not in path), "invalid API path")
        request = urllib.request.Request(self.root + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise Blocked(f"GitHub API returned HTTP {error.code}") from None

    def pages(self, path, field=None):
        result = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            data = self.request(f"{path}{separator}per_page=100&page={page}")
            values = data[field] if field else data
            require(isinstance(values, list), "invalid paginated evidence")
            result.extend(values)
            if len(values) < 100:
                return result
        raise Blocked("evidence pagination limit reached")


def policy(api, base):
    raw = api.request('/contents/.github/repair-policy.json?ref=' + sha(base))
    require(raw.get('encoding') == 'base64', 'unsupported repair policy encoding')
    config = json.loads(base64.b64decode(raw['content']))
    require(isinstance(config, dict) and type(config.get('schema')) is int
            and config['schema'] == 1, 'unsupported repair policy schema')
    require(isinstance(config.get('ci_workflow'), str) and re.fullmatch(
        r'[A-Za-z0-9_.-]+\.ya?ml', config['ci_workflow']), 'invalid CI workflow filename')
    require(isinstance(config.get('repair_recovery'), dict), 'missing repair recovery policy')
    return config


def is_renovate(user):
    return user.get('login') == 'renovate[bot]' and user.get('id') == 29139614 and user.get('type') == 'Bot'


def require_no_objections(pr, reviews):
    require(not pr.get('requested_reviewers') and not pr.get('requested_teams'), 'review requests remain outstanding')
    latest = {}
    for review in sorted(reviews, key=lambda item: item['id']):
        state = review.get('state')
        if state in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'}:
            latest[review['user']['id']] = review
        require(state != 'PENDING', 'a review remains pending')
    require(not any(r['state'] == 'CHANGES_REQUESTED' for r in latest.values()), 'changes are requested')
