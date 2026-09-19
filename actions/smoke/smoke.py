"""Own the lifetime of a local app while repository-owned smoke assertions run."""

import ipaddress
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class SmokeError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def duration(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise SmokeError('deadlines must be greater than zero and at most 3600 seconds')
    return seconds


def stop(process):
    """Terminate the whole group, including children left behind by its leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def wait_for_ready(app, url, timeout):
    # Socket timeouts restart on each read. A wall-clock alarm also bounds servers
    # that keep trickling response headers. This CLI runs on the main thread.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout
    last_error = 'no response'

    def expired(signum, frame):
        raise SmokeError(f'application readiness timed out: {last_error}')

    previous_handler = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, timeout)
        while True:
            if app.poll() is not None:
                raise SmokeError(f'application exited before readiness (exit {app.returncode})')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                expired(None, None)
            try:
                # Ignore proxies and redirects: readiness must come from this app.
                with client.open(url, timeout=min(1, remaining)) as response:
                    if 200 <= response.status < 300:
                        return
                    last_error = f'HTTP {response.status}'
            except urllib.error.HTTPError as error:
                last_error = str(error)
                error.close()
            except OSError as error:
                last_error = str(error)
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def run(start_command, url, test_command, working_directory='.', timeout=60, test_timeout=120):
    if sys.platform not in {'linux', 'darwin'}:
        raise SmokeError('smoke supports Linux and macOS runners')
    if not start_command.strip() or not test_command.strip():
        raise SmokeError('start-command and test-command must be nonempty')
    timeout, test_timeout = duration(timeout), duration(test_timeout)
    directory = Path(working_directory).resolve(strict=True)
    if not directory.is_dir():
        raise SmokeError('working-directory must be a directory')
    target = urllib.parse.urlsplit(url)
    host = target.hostname
    try:
        loopback = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if target.scheme not in {'http', 'https'} or not loopback or target.username or target.password:
        raise SmokeError('url must be an HTTP or HTTPS loopback URL without credentials')
    port = target.port or (443 if target.scheme == 'https' else 80)
    try:
        with socket.create_connection((host, port), timeout=1):
            raise SmokeError('readiness port already has a listener; refusing to test a stale app')
    except OSError:
        pass

    processes = []
    with tempfile.TemporaryFile() as log:
        failed = True
        try:
            def launch(command):
                process = subprocess.Popen(
                    ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', command],
                    cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                processes.append(process)
                return process

            app = launch(start_command)
            wait_for_ready(app, url, timeout)

            assertion = launch(test_command)
            deadline = time.monotonic() + test_timeout
            while True:
                if app.poll() is not None:
                    raise SmokeError(f'application exited during assertions (exit {app.returncode})')
                if assertion.poll() is not None:
                    if assertion.returncode:
                        raise SmokeError(f'application assertions failed (exit {assertion.returncode})')
                    break
                if time.monotonic() >= deadline:
                    raise SmokeError('application assertions timed out')
                time.sleep(0.05)
            failed = False
        finally:
            for process in reversed(processes):
                stop(process)
            if failed:
                log.seek(0)
                print('Application and assertion output:', flush=True)
                print(log.read().decode(errors='replace'), flush=True)
    print('Application readiness and smoke assertions passed.')


def interrupted(signum, frame):
    raise SmokeError(f'smoke interrupted by signal {signum}')


def main():
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        run(os.environ['SMOKE_START_COMMAND'], os.environ['SMOKE_URL'],
            os.environ['SMOKE_TEST_COMMAND'], os.environ.get('SMOKE_WORKING_DIRECTORY', '.'),
            os.environ.get('SMOKE_TIMEOUT_SECONDS', '60'),
            os.environ.get('SMOKE_TEST_TIMEOUT_SECONDS', '120'))
    except (KeyError, OSError, ValueError) as error:
        print(f'Smoke failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
