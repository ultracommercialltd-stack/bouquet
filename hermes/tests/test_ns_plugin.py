import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes import ns_puppetmaster
from hermes.ns_puppetmaster import onboard, plugin, tools


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self, limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PuppetMasterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.creds = Path(self.tmp.name) / "credentials.json"
        self.creds.write_text(
            json.dumps(
                {
                    "version": 1,
                    "main_nation": "united_paradisia",
                    "nations": {
                        "united_paradisia": {
                            "enabled": True,
                            "autologin": "secret-autologin",
                            "allow_issue_autonomy": False,
                        },
                        "sleepy_puppet": {
                            "enabled": True,
                            "autologin": "other-secret",
                            "allow_issue_autonomy": True,
                            "decision_profile": "preserve",
                        },
                    },
                }
            )
        )
        os.chmod(self.creds, 0o600)
        self.env = patch.dict(
            os.environ,
            {
                "NS_CREDENTIALS_FILE": str(self.creds),
                "NS_MAIN_NATION": "united_paradisia",
                "NS_USER_AGENT": "PuppetMaster/0.1 (by:United_Paradisia; usedBy:United_Paradisia)",
                "NS_ALLOW_AUTONOMOUS_ISSUES": "false",
                "NS_MAX_ACTIONS_PER_RUN": "20",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_canonical_nation_name(self):
        self.assertEqual(tools.canon(" United Paradisia "), "united_paradisia")

    def test_roster_status_never_returns_credentials(self):
        result = json.loads(tools.ns_roster_status({}))
        self.assertTrue(result["ok"])
        encoded = json.dumps(result)
        self.assertNotIn("secret-autologin", encoded)
        self.assertNotIn("other-secret", encoded)
        self.assertEqual(result["nations"][0]["nation"], "sleepy_puppet")
        self.assertEqual(result["nations"][1]["nation"], "united_paradisia")

    def test_unknown_nation_is_denied_before_network(self):
        result = json.loads(tools.ns_keepalive({"nations": ["intruder"]}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unauthorized_nation")

    def test_keepalive_uses_autologin_and_returns_safe_evidence(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            return FakeResponse(
                "<NATION><PING>1</PING></NATION>",
                headers={"RateLimit-Remaining": "49", "RateLimit-Reset": "30"},
            )

        with patch.object(tools.urllib.request, "urlopen", opener):
            result = json.loads(tools.ns_keepalive({"nations": ["United Paradisia"]}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["status"], "passed")
        self.assertIn("nation=united_paradisia", captured["url"])
        self.assertIn("q=ping", captured["url"])
        self.assertEqual(captured["headers"]["X-autologin"], "secret-autologin")
        self.assertNotIn("secret-autologin", json.dumps(result))

    def test_public_status_uses_combined_shards(self):
        xml = (
            "<NATION><NAME>United Paradisia</NAME><REGION>Test Region</REGION>"
            "<LASTACTIVITY>10 minutes ago</LASTACTIVITY><UNSTATUS>Non-member</UNSTATUS></NATION>"
        )
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse(xml, headers={"RateLimit-Remaining": "48", "RateLimit-Reset": "20"})

        with patch.object(tools.urllib.request, "urlopen", opener):
            result = json.loads(tools.ns_nation_status({"nation": "United Paradisia"}))

        self.assertTrue(result["ok"])
        self.assertIn("q=name%2Bregion%2Blastactivity%2Bunstatus", captured["url"])
        self.assertEqual(result["nation"]["region"], "Test Region")

    def test_issue_execution_is_denied_when_global_policy_is_off(self):
        result = json.loads(
            tools.ns_answer_issue({"nation": "united_paradisia", "issue_id": 123, "option_id": 1})
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "policy_denied")

    def test_main_nation_issue_execution_remains_manual(self):
        with patch.dict(os.environ, {"NS_ALLOW_AUTONOMOUS_ISSUES": "true"}):
            result = json.loads(
                tools.ns_answer_issue({"nation": "united_paradisia", "issue_id": 123, "option_id": 1})
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "main_nation_manual")

    def test_get_issues_returns_options_and_assigned_profile_without_secret(self):
        xml = (
            "<NATION><ISSUES><ISSUE id='123'><TITLE>A Choice</TITLE><TEXT>Choose.</TEXT>"
            "<OPTION id='0'>First</OPTION><OPTION id='1'>Second</OPTION>"
            "</ISSUE></ISSUES></NATION>"
        )

        def opener(request, timeout):
            self.assertEqual(dict(request.header_items())["X-autologin"], "other-secret")
            return FakeResponse(xml, headers={"RateLimit-Remaining": "40", "RateLimit-Reset": "20"})

        with patch.object(tools.urllib.request, "urlopen", opener):
            result = json.loads(tools.ns_get_issues({"nation": "sleepy_puppet"}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision_profile"], "preserve")
        self.assertEqual(result["issues"][0]["options"][1], {"id": 1, "text": "Second"})
        self.assertNotIn("other-secret", json.dumps(result))

    def test_autonomous_issue_executes_for_enabled_non_main_puppet(self):
        captured = {}

        def opener(request, timeout):
            captured["data"] = request.data.decode("utf-8")
            captured["headers"] = dict(request.header_items())
            return FakeResponse(
                "<NATION><OK>1</OK><DESC>Policy enacted.</DESC></NATION>",
                headers={"RateLimit-Remaining": "39", "RateLimit-Reset": "20"},
            )

        with (
            patch.dict(os.environ, {"NS_ALLOW_AUTONOMOUS_ISSUES": "true"}),
            patch.object(tools.urllib.request, "urlopen", opener),
        ):
            result = json.loads(
                tools.ns_answer_issue({"nation": "sleepy_puppet", "issue_id": 123, "option_id": 1})
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "passed")
        self.assertIn("c=issue", captured["data"])
        self.assertIn("issue=123", captured["data"])
        self.assertIn("option=1", captured["data"])
        self.assertEqual(captured["headers"]["X-autologin"], "other-secret")
        self.assertNotIn("other-secret", json.dumps(result))

    def test_policy_marks_html_restricted_actions_manual_only(self):
        result = json.loads(tools.ns_action_policy({"action": "move_region"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "manual_only")
        self.assertEqual(result["executor"], "Bouquet")

    def test_policy_denies_html_telegram_sending(self):
        result = json.loads(tools.ns_action_policy({"action": "send_telegram_html"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "denied")
    def test_missing_rate_headers_are_safe(self):
        self.assertEqual(
            tools._rate_headers(None),
            {"limit": None, "remaining": None, "reset_seconds": None, "retry_after_seconds": None},
        )

    def test_rate_limiter_waits_full_retry_after_before_next_request(self):
        tools.reset_rate_limiter_for_tests()
        responses = [
            FakeResponse("<NATION><NAME>One</NAME></NATION>", headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "120"}),
            FakeResponse("<NATION><NAME>Two</NAME></NATION>", headers={"RateLimit-Remaining": "49", "RateLimit-Reset": "30"}),
        ]
        clock = {"now": 100.0}
        sleeps = []

        def opener(request, timeout):
            return responses.pop(0)

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        with (
            patch.object(tools.urllib.request, "urlopen", opener),
            patch.object(tools.time, "monotonic", lambda: clock["now"]),
            patch.object(tools.time, "sleep", fake_sleep),
        ):
            first = json.loads(tools.ns_nation_status({"nation": "One"}))
            second = json.loads(tools.ns_nation_status({"nation": "Two"}))

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(sleeps, [121.0])

    def test_onboarding_creates_secret_temp_file_as_0600(self):
        seen_modes = []
        real_open = os.open

        def checked_open(path, flags, mode=0o777):
            if str(path).endswith(".tmp"):
                seen_modes.append(mode)
            return real_open(path, flags, mode)

        with patch.object(onboard.os, "open", checked_open):
            onboard._atomic_write_credentials(self.creds, {"version": 1, "main_nation": "x", "nations": {}})

        self.assertEqual(seen_modes, [0o600])
        self.assertEqual(self.creds.stat().st_mode & 0o777, 0o600)

    def test_onboarding_exchanges_password_for_autologin_without_storing_password(self):
        def opener(request, timeout):
            self.assertEqual(dict(request.header_items())["X-password"], "one-time-password")
            return FakeResponse(
                "<NATION><PING>1</PING></NATION>",
                headers={"X-Autologin": "new-autologin", "X-Pin": "temporary-pin"},
            )

        with patch.object(onboard.urllib.request, "urlopen", opener):
            result = onboard.add_nation(
                self.creds,
                "New Puppet",
                "one-time-password",
                user_agent="PuppetMaster/0.1 (by:United_Paradisia; usedBy:United_Paradisia)",
            )

        stored = self.creds.read_text()
        self.assertEqual(result["nation"], "new_puppet")
        self.assertNotIn("one-time-password", stored)
        self.assertNotIn("temporary-pin", stored)
        self.assertIn("new-autologin", stored)
        self.assertEqual(self.creds.stat().st_mode & 0o777, 0o600)

    def test_onboarding_can_assign_autonomous_decision_profile(self):
        def opener(request, timeout):
            return FakeResponse(
                "<NATION><PING>1</PING></NATION>",
                headers={"X-Autologin": "profile-autologin"},
            )

        with patch.object(onboard.urllib.request, "urlopen", opener):
            onboard.add_nation(
                self.creds,
                "Profile Puppet",
                "one-time-password",
                user_agent="PuppetMaster/0.1 (by:United_Paradisia; usedBy:United_Paradisia)",
                allow_issue_autonomy=True,
                decision_profile="mirror_united_paradisia",
            )

        stored = json.loads(self.creds.read_text())
        puppet = stored["nations"]["profile_puppet"]
        self.assertTrue(puppet["allow_issue_autonomy"])
        self.assertEqual(puppet["decision_profile"], "mirror_united_paradisia")

    def test_plugin_registers_only_guarded_ns_tools(self):
        self.assertTrue(callable(ns_puppetmaster.register))

        class Ctx:
            def __init__(self):
                self.tools = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

        ctx = Ctx()
        plugin.register(ctx)
        names = [item["name"] for item in ctx.tools]
        self.assertEqual(
            names,
            ["ns_roster_status", "ns_nation_status", "ns_keepalive", "ns_get_issues", "ns_answer_issue", "ns_action_policy"],
        )
        for item in ctx.tools:
            self.assertEqual(item["toolset"], "ns-puppetmaster")
            self.assertFalse(item["schema"]["parameters"].get("additionalProperties", True))


if __name__ == "__main__":
    unittest.main()
