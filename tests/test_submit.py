from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fixtures import SERVER_FIXTURE, build_projects, demo_records

from tokencount import remote
from tokencount.cli import main
from tokencount.policy import check_record, load_policy, screen
from tokencount.pricing import PriceTable

CASES = json.loads((Path(__file__).with_name("policy_cases.json")).read_text(encoding="utf-8"))


class FakeServer:
    """A local HTTP server; `respond(method, path, body, headers)` returns (status, json)."""

    def __init__(self, respond):
        self.requests: list[tuple[str, str, dict, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    body = json.loads(raw or b"{}")
                else:
                    body = dict(p.split("=", 1) for p in raw.decode().split("&") if "=" in p)
                headers = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append((method, self.path, body, headers))
                status, payload = respond(method, self.path, body, headers)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def accept_all(method, path, body, headers):
    return 200, {"results": [{"record_id": r["record_id"], "status": "published", "new": True, "reasons": []}
                             for r in body["records"]]}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = str(build_projects(self.root))
        env = {"TOKENCOUNT_HOME": str(self.root / "home"), "TOKENCOUNT_API": "", "TOKENCOUNT_GITHUB_CLIENT_ID": ""}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.servers: list[FakeServer] = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        self.env.stop()
        self.tmp.cleanup()

    def server(self, respond) -> FakeServer:
        server = FakeServer(respond)
        self.servers.append(server)
        return server

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(list(argv))
        return code, out.getvalue()


class SubmitTests(CliCase):
    def submit(self, api: FakeServer, *extra: str) -> tuple[int, str]:
        return self.run_cli("submit", "--path", self.projects, "--api", api.url, "--yes", *extra)

    def test_sends_records_once_and_resends_only_with_all(self):
        api = self.server(accept_all)
        code, out = self.submit(api)
        self.assertEqual(code, 0, out)
        self.assertIn("Sent: 4 new, 0 updated, 0 held for review, 0 rejected.", out)
        self.assertIn("sending anonymously", out)
        (method, path, body, headers), = api.requests
        self.assertEqual((method, path, len(body["records"])), ("POST", "/v1/records", 4))
        self.assertRegex(headers["x-contributor-key"], r"^[0-9a-f]{64}$")
        self.assertNotIn("authorization", headers)
        self.assertTrue(headers["user-agent"].startswith("tokencount/"))

        code, out = self.submit(api)
        self.assertIn("Nothing new to send.", out)
        self.assertEqual(len(api.requests), 1)

        self.submit(api, "--all")
        self.assertEqual(len(api.requests), 2)

    def test_large_submissions_go_in_batches(self):
        api = self.server(accept_all)
        with mock.patch.object(remote, "BATCH_SIZE", 3):
            code, out = self.submit(api)
        self.assertEqual([len(r[2]["records"]) for r in api.requests], [3, 1])
        self.assertIn("Sent: 4 new", out)

    def test_upgrading_tokencount_does_not_resend_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = demo_records(Path(tmp))[0]
        newer = dict(record, importer="tokencount/9.9.9")
        self.assertEqual(remote.digest(record), remote.digest(newer))
        self.assertNotEqual(remote.digest(record), remote.digest(dict(record, turns=record["turns"] + 1)))

    def test_logged_in_submissions_carry_the_github_token(self):
        remote.save_github("gho_test", "octo")
        api = self.server(accept_all)
        code, out = self.submit(api)
        self.assertIn("sending as GitHub @octo (verified)", out)
        self.assertEqual(api.requests[0][3]["authorization"], "Bearer gho_test")

    def test_rejected_records_are_reported_and_sent_again_next_time(self):
        def reject_first(method, path, body, headers):
            first, *rest = body["records"]
            results = [{"record_id": first["record_id"], "status": "rejected", "reasons": ["date is in the future"]}]
            results += [{"record_id": r["record_id"], "status": "flagged", "new": True, "reasons": ["x"]} for r in rest]
            return 200, {"results": results}

        api = self.server(reject_first)
        code, out = self.submit(api)
        self.assertEqual(code, 1)
        self.assertIn("0 new, 0 updated, 3 held for review, 1 rejected", out)
        self.assertIn("date is in the future", out)
        self.submit(api)
        self.assertEqual(len(api.requests[1][2]["records"]), 1)

    def test_rate_limit_and_bad_login_stop_with_an_error(self):
        api = self.server(lambda *a: (429, {"error": "Daily limit of 500 records reached."}))
        code, out = self.submit(api)
        self.assertEqual(code, 1)
        self.assertIn("Daily limit of 500 records reached.", out)
        api = self.server(lambda *a: (401, {"error": "bad token"}))
        code, out = self.submit(api)
        self.assertEqual(code, 1)
        self.assertIn("tokencount login", out)

    def test_dry_run_and_no_tty_send_nothing(self):
        api = self.server(accept_all)
        code, out = self.run_cli("submit", "--path", self.projects, "--api", api.url, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run, nothing sent.", out)
        with mock.patch("sys.stdin", io.StringIO("")):
            code, out = self.run_cli("submit", "--path", self.projects, "--api", api.url)
        self.assertEqual(code, 2)
        self.assertIn("--yes", out)
        self.assertEqual(api.requests, [])

    def test_bad_descriptions_are_held_back_locally(self):
        code, out = self.run_cli("tasks", "--path", self.projects)
        task_id = next(line.split()[0] for line in out.splitlines() if "Rename the export button" in line)
        code, out = self.run_cli("label", task_id, "--path", self.projects, "--description",
                                 "rename button, ask jane@example.com")
        self.assertIn("Warning: description contains an e-mail address", out)
        api = self.server(accept_all)
        code, out = self.submit(api)
        self.assertIn("1 record(s) not sent because of problems", out)
        self.assertEqual(len(api.requests[0][2]["records"]), 3)

    def test_no_api_configured(self):
        code, out = self.run_cli("submit", "--path", self.projects, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("No API is configured", out)


class LoginTests(CliCase):
    def test_device_flow_stores_the_token_and_login(self):
        polls = []

        def github(method, path, body, headers):
            if path == "/login/device/code":
                return 200, {"device_code": "dev123", "user_code": "WDJB-MJHT",
                             "verification_uri": "https://github.com/login/device", "interval": 0, "expires_in": 60}
            if path == "/login/oauth/access_token":
                polls.append(body)
                if len(polls) == 1:
                    return 200, {"error": "authorization_pending"}
                return 200, {"access_token": "gho_device", "token_type": "bearer", "scope": ""}
            if path == "/user":
                self.assertEqual(headers["authorization"], "Bearer gho_device")
                return 200, {"login": "octo", "id": 42}
            return 404, {}

        gh = self.server(github)
        env = {"TOKENCOUNT_GITHUB_CLIENT_ID": "Iv1.test", "TOKENCOUNT_GITHUB_URL": gh.url,
               "TOKENCOUNT_GITHUB_API_URL": gh.url}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_cli("login")
        self.assertEqual(code, 0, out)
        self.assertIn("enter the code WDJB-MJHT", out)
        self.assertEqual(polls[-1]["device_code"], "dev123")
        self.assertEqual(remote.load_github(), {"token": "gho_device", "login": "octo"})
        self.assertEqual(self.run_cli("logout")[1].strip(), "Logged out.")
        self.assertIsNone(remote.load_github())

    def test_login_needs_an_oauth_app(self):
        code, out = self.run_cli("login")
        self.assertEqual(code, 2)
        self.assertIn("isn't set up yet", out)


class PolicyTests(unittest.TestCase):
    policy = load_policy()

    def problems(self, description: str) -> list[str]:
        return screen({"task": {"description": description}}, self.policy)

    def test_screening_catches_personal_and_secret_data(self):
        for text, expected in CASES["catch"]:
            self.assertIn(f"description contains {expected}", self.problems(text), text)

    def test_screening_leaves_ordinary_descriptions_alone(self):
        for text in CASES["allow"]:
            self.assertEqual(self.problems(text), [], text)

    def test_prompt_text_is_only_screened_for_secrets(self):
        record = {"task": {"prompt": "open /home/jane/app.py and https://example.com", "description": None}}
        self.assertEqual(screen(record, self.policy), [])
        record["task"]["prompt"] = "use key AKIAABCDEFGHIJKLMNOP"
        self.assertEqual(screen(record, self.policy), ["prompt contains an API key or token"])

    def test_check_record_errors_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = demo_records(Path(tmp))[1]
        prices = PriceTable.load()
        self.assertEqual(check_record(record, prices=prices, today=date(2026, 11, 12)).errors, [])

        broken = json.loads(json.dumps(record))
        broken["turns"] += 1
        broken["calls"][0]["cache_read"] = 3_000_000
        broken["date"] = "2027-01-01"
        errors = check_record(broken, prices=prices, today=date(2026, 11, 12)).errors
        self.assertIn("turns does not match the number of main-thread calls", errors)
        self.assertIn("calls[0]: more input tokens than any model accepts", errors)
        self.assertIn("date is in the future", errors)

        odd = json.loads(json.dumps(record))
        odd["calls"][0]["model"] = "brand-new-model"
        self.assertEqual(check_record(odd, prices=prices, today=date(2026, 11, 12)).flags,
                         ["no price for brand-new-model"])

        big_call = dict(record["calls"][0], role="main", model="claude-opus-5-5", input_fresh=0, cache_write_5m=0,
                        cache_write_1h=1_900_000, cache_read=0, output=1000, thinking=0)  # about $15.22 each
        pricey = dict(record, calls=[dict(big_call) for _ in range(100)], turns=100, subagent_calls=0,
                      peak_context_tokens=1_900_000)
        self.assertEqual(check_record(pricey, prices=prices, today=date(2026, 11, 12)).flags,
                         ["costs more than $1,000"])


class SharedFixtureTests(unittest.TestCase):
    def test_api_fixture_matches_the_exporter(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = [json.loads(json.dumps(r)) for r in demo_records(Path(tmp))]
        actual = [json.loads(line) for line in SERVER_FIXTURE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(actual, expected, "Run `python3 tests/fixtures.py` to refresh server/test/fixtures.")


if __name__ == "__main__":
    unittest.main()
