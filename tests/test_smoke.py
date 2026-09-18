import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('smoke', Path(__file__).parents[1] / 'actions/smoke/smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

SERVER = '''
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import os
import signal
import socket
import subprocess
import sys
import threading
import time

mode, port = sys.argv[1], int(sys.argv[2])
if mode == 'child':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    listener = socket.socket()
    listener.bind(('127.0.0.1', port))
    listener.listen()
    Path('child-ready').touch()
    time.sleep(60)
    sys.exit()
if len(sys.argv) > 3:
    subprocess.Popen([sys.executable, __file__, 'child', sys.argv[3]])
    while not Path('child-ready').exists():
        time.sleep(0.01)

class Handler(BaseHTTPRequestHandler):
    attempts = 0
    def do_GET(self):
        Handler.attempts += 1
        if mode == 'slow-headers':
            print('fixture trickling headers', flush=True)
            self.wfile.write(b'HTTP/1.0 200 OK\\r\\nX-Progress: ')
            self.wfile.flush()
            for _ in range(50):
                self.wfile.write(b'x')
                self.wfile.flush()
                time.sleep(0.03)
            self.wfile.write(b'\\r\\n\\r\\n<main>Application loaded</main>')
            return
        status = 200
        if mode == 'error' or (mode == 'warming' and Handler.attempts < 3):
            status = 503
        if mode == 'redirect' and self.path != '/ok':
            status = 302
        self.send_response(status)
        if status == 302:
            self.send_header('Location', '/ok')
        self.end_headers()
        self.wfile.write(b'<main>Application loaded</main>')
        if mode == 'dies':
            threading.Timer(0.15, lambda: os._exit(7)).start()

print('fixture application started', flush=True)
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
'''


@unittest.skipUnless(sys.platform in {'linux', 'darwin'}, 'POSIX process groups required')
class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.fixture = self.directory / 'server.py'
        self.fixture.write_text(SERVER)
        self.port = self.free_port()
        self.url = f'http://127.0.0.1:{self.port}/'
        self.output = io.StringIO()

    def free_port(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]

    def command(self, code):
        return shlex.join([sys.executable, '-c', code])

    def start(self, mode='ready', child_port=None):
        args = [sys.executable, str(self.fixture), mode, str(self.port)]
        if child_port is not None:
            args.append(str(child_port))
        return shlex.join(args)

    def assertion(self):
        return self.command(
            f'import urllib.request; assert b"<main>Application loaded</main>" in '
            f'urllib.request.urlopen({self.url!r}).read()'
        )

    def run_smoke(self, start=None, assertion=None, timeout=2, test_timeout=2):
        with contextlib.redirect_stdout(self.output):
            smoke.run(start or self.start(), self.url, assertion or self.assertion(),
                      self.directory, timeout, test_timeout)

    def assert_port_closed(self, port):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                    pass
            except OSError:
                return
            time.sleep(0.01)
        self.fail(f'process is still listening on port {port}')

    def wait_for_listener(self, port):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.01)
        self.fail(f'fixture did not listen on port {port}')

    def test_application_content_assertion_and_cleanup(self):
        self.run_smoke()
        self.assertIn('smoke assertions passed', self.output.getvalue())
        self.assert_port_closed(self.port)

    def test_readiness_retries_until_application_is_ready(self):
        self.run_smoke(start=self.start('warming'))
        self.assert_port_closed(self.port)

    def test_both_commands_inherit_caller_environment_and_working_directory(self):
        check = self.command(
            'import os; from pathlib import Path; '
            'assert os.environ["CALLER_SMOKE_VALUE"] == "caller-owned"; '
            'assert Path("server.py").is_file()'
        )
        with patch.dict(os.environ, CALLER_SMOKE_VALUE='caller-owned'):
            self.run_smoke(start=check + ' && ' + self.start(),
                           assertion=check + ' && ' + self.assertion())

    def test_preexisting_listener_is_rejected_and_left_alone(self):
        fixture = subprocess.Popen(shlex.split(self.start()), cwd=self.directory,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.addCleanup(smoke.stop, fixture)
        self.wait_for_listener(self.port)
        with self.assertRaisesRegex(smoke.SmokeError, 'already has a listener'):
            self.run_smoke()
        self.assertIsNone(fixture.poll())

    def test_early_application_exit_reports_diagnostics(self):
        with self.assertRaisesRegex(smoke.SmokeError, 'before readiness.*exit 9'):
            self.run_smoke(start='echo startup-failed; exit 9')
        self.assertIn('startup-failed', self.output.getvalue())

    def test_startup_timeout_cleans_up_process(self):
        previous_handler = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(smoke.SmokeError, 'readiness timed out'):
            self.run_smoke(start=self.command('import time; print("waiting", flush=True); time.sleep(60)'),
                           timeout=0.2)
        self.assertIn('waiting', self.output.getvalue())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        self.assertIs(signal.getsignal(signal.SIGALRM), previous_handler)

    def test_trickling_response_headers_cannot_extend_startup_deadline(self):
        environment = dict(os.environ, SMOKE_START_COMMAND=self.start('slow-headers'),
                           SMOKE_URL=self.url, SMOKE_TEST_COMMAND='true',
                           SMOKE_WORKING_DIRECTORY=str(self.directory), SMOKE_TIMEOUT_SECONDS='0.25')
        started = time.monotonic()
        process = subprocess.run([sys.executable, str(Path(smoke.__file__))], env=environment,
                                 capture_output=True, timeout=3)
        elapsed = time.monotonic() - started
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'fixture trickling headers', process.stdout)
        self.assertIn(b'readiness timed out', process.stderr)
        self.assertLess(elapsed, 1, 'the startup deadline must bound the whole HTTP response')
        self.assert_port_closed(self.port)

    def test_http_errors_and_redirects_do_not_count_as_readiness(self):
        for mode, status in [('error', 503), ('redirect', 302)]:
            with self.subTest(mode=mode), self.assertRaisesRegex(smoke.SmokeError, f'HTTP Error {status}'):
                self.run_smoke(start=self.start(mode), timeout=0.3)
            self.assert_port_closed(self.port)

    def test_failed_assertion_reports_output_and_cleans_up(self):
        with self.assertRaisesRegex(smoke.SmokeError, 'assertions failed.*exit 3'):
            self.run_smoke(assertion='echo missing-app-landmark; exit 3')
        self.assertIn('missing-app-landmark', self.output.getvalue())
        self.assertIn('fixture application started', self.output.getvalue())
        self.assert_port_closed(self.port)

    def test_assertion_timeout_cleans_up_both_process_groups(self):
        child_port = self.free_port()
        child = shlex.join([sys.executable, str(self.fixture), 'child', str(child_port)])
        with self.assertRaisesRegex(smoke.SmokeError, 'assertions timed out'):
            self.run_smoke(assertion=child, test_timeout=0.2)
        self.assert_port_closed(child_port)
        self.assert_port_closed(self.port)

    def test_application_exit_during_assertions_fails(self):
        with self.assertRaisesRegex(smoke.SmokeError, 'during assertions.*exit 7'):
            self.run_smoke(start=self.start('dies'), assertion=self.command('import time; time.sleep(60)'))
        self.assert_port_closed(self.port)

    def test_cleanup_kills_descendants_that_ignore_sigterm(self):
        child_port = self.free_port()
        self.run_smoke(start=self.start(child_port=child_port))
        self.assert_port_closed(child_port)
        self.assert_port_closed(self.port)

    def test_sigterm_cleans_up_application_group(self):
        child_port = self.free_port()
        environment = dict(os.environ, SMOKE_START_COMMAND=self.start(child_port=child_port),
                           SMOKE_URL=self.url, SMOKE_TEST_COMMAND=self.command('import time; time.sleep(60)'),
                           SMOKE_WORKING_DIRECTORY=str(self.directory))
        process = subprocess.Popen([sys.executable, str(Path(smoke.__file__))], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.wait_for_listener(self.port)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'interrupted by signal', output)
        self.assert_port_closed(child_port)
        self.assert_port_closed(self.port)

    def test_invalid_inputs_fail_before_launch(self):
        for url in ['https://example.com/', 'file:///tmp/app', 'http://user:pass@localhost/']:
            with self.subTest(url=url), self.assertRaisesRegex(smoke.SmokeError, 'loopback URL'):
                smoke.run('true', url, 'true')
        for timeout in [0, -1, 'nan', 'inf', 3601]:
            with self.subTest(timeout=timeout), self.assertRaisesRegex(smoke.SmokeError, 'deadlines'):
                smoke.run('true', self.url, 'true', timeout=timeout)
        for start, assertion in [(' ', 'true'), ('true', ' ')]:
            with self.assertRaisesRegex(smoke.SmokeError, 'must be nonempty'):
                smoke.run(start, self.url, assertion)
