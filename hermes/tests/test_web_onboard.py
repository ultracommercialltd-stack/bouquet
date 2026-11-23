import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes.ns_puppetmaster import web_onboard


class WebOnboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "web-state.json"
        self.credentials = self.root / "credentials.json"
        self.credentials.write_text(
            json.dumps({"version": 1, "main_nation": "united_paradisia", "nations": {}}),
            encoding="utf-8",
        )
        os.chmod(self.credentials, 0o600)
        self.manager = web_onboard.OnboardingManager(
            state_file=self.state,
            credentials_file=self.credentials,
            user_agent="PuppetMaster/0.1 (by:United_Paradisia; usedBy:United_Paradisia)",
            public_url="https://example.invalid",
            now=lambda: 1_000.0,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_issued_link_is_short_lived_and_state_starts_0600(self):
        link = self.manager.issue_link(ttl_seconds=600)
        self.assertTrue(link.startswith("https://example.invalid/start?token="))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        stored = self.state.read_text(encoding="utf-8")
        token = link.split("token=", 1)[1]
        self.assertNotIn(token, stored)
        self.assertNotIn("password", stored.lower())

    def test_link_exchanges_once_for_cookie_session_and_csrf(self):
        token = self.manager.issue_link(ttl_seconds=600).split("token=", 1)[1]
        session = self.manager.exchange_link(token)
        self.assertIsNotNone(session)
        self.assertIn("cookie", session)
        self.assertIn("csrf", session)
        self.assertIsNone(self.manager.exchange_link(token))
        self.assertNotIn(token, self.state.read_text(encoding="utf-8"))

    def test_expired_link_is_rejected(self):
        token = self.manager.issue_link(ttl_seconds=60).split("token=", 1)[1]
        self.manager.now = lambda: 1_061.0
        self.assertIsNone(self.manager.exchange_link(token))

    def test_submit_requires_origin_cookie_csrf_and_never_persists_password(self):
        token = self.manager.issue_link(ttl_seconds=600).split("token=", 1)[1]
        session = self.manager.exchange_link(token)
        with patch.object(web_onboard.onboard, "add_nation", return_value={"nation": "test_puppet", "status": "added"}) as add:
            denied = self.manager.submit(
                cookie=session["cookie"], csrf="wrong", origin="https://example.invalid",
                nation="Test Puppet", password="super-secret-password",
                decision_profile="preserve", census_target="",
            )
            result = self.manager.submit(
                cookie=session["cookie"], csrf=session["csrf"], origin="https://example.invalid",
                nation="Test Puppet", password="super-secret-password",
                decision_profile="preserve", census_target="",
            )
        self.assertFalse(denied["ok"])
        self.assertTrue(result["ok"])
        self.assertEqual(add.call_args.args[2], "super-secret-password")
        self.assertNotIn("super-secret-password", self.state.read_text(encoding="utf-8"))

    def test_main_nation_is_forced_manual(self):
        token = self.manager.issue_link(ttl_seconds=600).split("token=", 1)[1]
        session = self.manager.exchange_link(token)
        with patch.object(web_onboard.onboard, "add_nation", return_value={"nation": "united_paradisia", "status": "added"}) as add:
            result = self.manager.submit(
                cookie=session["cookie"], csrf=session["csrf"], origin="https://example.invalid",
                nation="United Paradisia", password="secret",
                decision_profile="mirror_united_paradisia", census_target="",
            )
        self.assertTrue(result["ok"])
        self.assertFalse(add.call_args.kwargs["allow_issue_autonomy"])
        self.assertEqual(add.call_args.kwargs["decision_profile"], "manual")

    def test_census_profile_requires_target(self):
        token = self.manager.issue_link(ttl_seconds=600).split("token=", 1)[1]
        session = self.manager.exchange_link(token)
        result = self.manager.submit(
            cookie=session["cookie"], csrf=session["csrf"], origin="https://example.invalid",
            nation="Stats Puppet", password="secret",
            decision_profile="census_target", census_target="",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_profile")


if __name__ == "__main__":
    unittest.main()
