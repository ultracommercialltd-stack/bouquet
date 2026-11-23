"""Single-use HTTPS onboarding for NationStates puppet credentials."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

try:
    from . import onboard
except ImportError:  # Direct script execution from the installed plugin directory.
    import onboard  # type: ignore

COOKIE_NAME = "__Host-ns_onboard"
MAX_BODY_BYTES = 8192
MAX_LINK_TTL_SECONDS = 900
SESSION_TTL_SECONDS = 900
MAX_ATTEMPTS = 3
VALID_PROFILES = {"manual", "preserve", "mirror_united_paradisia", "census_target"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OnboardingManager:
    def __init__(
        self,
        *,
        state_file: Path,
        credentials_file: Path,
        user_agent: str,
        public_url: str = "",
        public_url_file: Path | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.state_file = state_file
        self.lock_file = state_file.with_suffix(state_file.suffix + ".lock")
        self.credentials_file = credentials_file
        self.user_agent = user_agent
        self.public_url = public_url.rstrip("/")
        self.public_url_file = public_url_file
        self.now = now
        self._thread_lock = threading.Lock()

    def current_public_url(self) -> str:
        if self.public_url_file and self.public_url_file.is_file():
            value = self.public_url_file.read_text(encoding="utf-8").strip().rstrip("/")
            if value.startswith("https://"):
                return value
        if self.public_url.startswith("https://"):
            return self.public_url
        raise RuntimeError("HTTPS public URL is not ready")

    def issue_link(self, ttl_seconds: int = 600) -> str:
        ttl = max(60, min(int(ttl_seconds), MAX_LINK_TTL_SECONDS))
        token = secrets.token_urlsafe(32)
        token_hash = _digest(token)
        now = self.now()

        def mutate(state: dict[str, Any]) -> None:
            self._prune(state, now)
            state["links"][token_hash] = {"expires_at": now + ttl, "used": False}

        self._mutate(mutate)
        return f"{self.current_public_url()}/start?token={quote(token)}"

    def exchange_link(self, token: str) -> dict[str, str] | None:
        if not token or len(token) > 128:
            return None
        token_hash = _digest(token)
        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self.now()
        result: dict[str, str] | None = None

        def mutate(state: dict[str, Any]) -> None:
            nonlocal result
            self._prune(state, now)
            link = state["links"].get(token_hash)
            if not link or link.get("used") or float(link.get("expires_at", 0)) <= now:
                return
            link["used"] = True
            state["sessions"][_digest(cookie)] = {
                "csrf_hash": _digest(csrf),
                "expires_at": now + SESSION_TTL_SECONDS,
                "used": False,
                "in_flight": False,
                "attempts": 0,
            }
            result = {"cookie": cookie, "csrf": csrf}

        self._mutate(mutate)
        return result

    def link_available(self, token: str) -> bool:
        if not token or len(token) > 128:
            return False
        link = self._read()["links"].get(_digest(token))
        return bool(link and not link.get("used") and float(link.get("expires_at", 0)) > self.now())

    def get_session(self, cookie: str) -> dict[str, Any] | None:
        if not cookie:
            return None
        state = self._read()
        session = state["sessions"].get(_digest(cookie))
        now = self.now()
        if not session or session.get("used") or session.get("in_flight"):
            return None
        if float(session.get("expires_at", 0)) <= now:
            return None
        return dict(session)

    def submit(
        self,
        *,
        cookie: str,
        csrf: str,
        origin: str,
        nation: str,
        password: str,
        decision_profile: str,
        census_target: str,
    ) -> dict[str, Any]:
        try:
            expected_origin = self.current_public_url()
        except RuntimeError:
            return {"ok": False, "code": "unavailable"}
        if origin.rstrip("/") != expected_origin:
            return {"ok": False, "code": "origin_denied"}
        if not cookie or not csrf:
            return {"ok": False, "code": "session_denied"}
        if decision_profile not in VALID_PROFILES:
            return {"ok": False, "code": "invalid_profile"}
        if decision_profile == "census_target" and not census_target.strip():
            return {"ok": False, "code": "invalid_profile"}
        if not nation.strip() or not password:
            return {"ok": False, "code": "invalid_input"}

        session_hash = _digest(cookie)
        csrf_hash = _digest(csrf)
        claimed = False
        now = self.now()

        def claim(state: dict[str, Any]) -> None:
            nonlocal claimed
            self._prune(state, now)
            session = state["sessions"].get(session_hash)
            if not session or session.get("used") or session.get("in_flight"):
                return
            if not secrets.compare_digest(str(session.get("csrf_hash", "")), csrf_hash):
                return
            attempts = int(session.get("attempts", 0))
            if attempts >= MAX_ATTEMPTS:
                return
            session["attempts"] = attempts + 1
            session["in_flight"] = True
            claimed = True

        self._mutate(claim)
        if not claimed:
            return {"ok": False, "code": "session_denied"}

        canonical = onboard.canon(nation)
        main = self._main_nation()
        profile = decision_profile
        allow_autonomy = profile != "manual"
        if canonical == main:
            profile = "manual"
            allow_autonomy = False
            census_target = ""
        try:
            result = onboard.add_nation(
                self.credentials_file,
                nation,
                password,
                user_agent=self.user_agent,
                allow_issue_autonomy=allow_autonomy,
                decision_profile=profile,
                census_target=census_target or None,
            )
        except Exception:
            self._finish_session(session_hash, success=False)
            return {"ok": False, "code": "onboarding_failed"}
        self._finish_session(session_hash, success=True)
        return {"ok": True, "nation": result["nation"], "decision_profile": profile}

    def _main_nation(self) -> str:
        try:
            data = json.loads(self.credentials_file.read_text(encoding="utf-8"))
            return onboard.canon(str(data.get("main_nation", "")))
        except (OSError, json.JSONDecodeError):
            return onboard.canon(os.environ.get("NS_MAIN_NATION", ""))

    def _finish_session(self, session_hash: str, *, success: bool) -> None:
        def mutate(state: dict[str, Any]) -> None:
            session = state["sessions"].get(session_hash)
            if not session:
                return
            session["in_flight"] = False
            if success or int(session.get("attempts", 0)) >= MAX_ATTEMPTS:
                session["used"] = True

        self._mutate(mutate)

    def _default_state(self) -> dict[str, Any]:
        return {"version": 1, "links": {}, "sessions": {}}

    def _read(self) -> dict[str, Any]:
        if not self.state_file.is_file():
            return self._default_state()
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default_state()
        if not isinstance(data, dict):
            return self._default_state()
        data.setdefault("links", {})
        data.setdefault("sessions", {})
        return data

    def _mutate(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_file, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            with self._thread_lock, os.fdopen(lock_fd, "w", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                state = self._read()
                callback(state)
                self._write(state)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def _write(self, state: dict[str, Any]) -> None:
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_file)
        os.chmod(self.state_file, 0o600)

    @staticmethod
    def _prune(state: dict[str, Any], now: float) -> None:
        for section in ("links", "sessions"):
            items = state.setdefault(section, {})
            stale = [key for key, value in items.items() if float(value.get("expires_at", 0)) <= now]
            for key in stale:
                items.pop(key, None)


def _security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
    handler.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    handler.send_header("Strict-Transport-Security", "max-age=31536000")


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui;background:#10131a;color:#f5f7ff;max-width:640px;margin:3rem auto;padding:1rem}"
        "main{background:#1b2130;padding:1.5rem;border-radius:16px}label{display:block;margin-top:1rem}"
        "input,select,button{box-sizing:border-box;width:100%;padding:.8rem;margin-top:.35rem;border-radius:8px;border:1px solid #59657a}"
        "button{background:#7c5cff;color:white;font-weight:700;border:0;margin-top:1.25rem}small{color:#b8c1d1}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    ).encode("utf-8")


def make_handler(manager: OnboardingManager):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PuppetOnboard/1"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # Never log paths, query tokens, nation names, or submitted form data.
            del format, args
            return

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", extra: list[tuple[str, str]] | None = None) -> None:
            self.send_response(status)
            _security_headers(self)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra or []:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send(HTTPStatus.OK, b'{"ok":true}', "application/json")
                return
            if parsed.path == "/start":
                token = parse_qs(parsed.query).get("token", [""])[0]
                if not manager.link_available(token):
                    self._send(HTTPStatus.NOT_FOUND, _page("Link unavailable", "<p>This link is invalid, expired, or already used.</p>"))
                    return
                body = (
                    "<p>This is a single-use Puppet Master onboarding link. Continue only if you requested it.</p>"
                    f"<form method='post' action='/exchange'><input type='hidden' name='token' value='{html.escape(token)}'>"
                    "<button type='submit'>Continue securely</button></form>"
                )
                self._send(HTTPStatus.OK, _page("Confirm onboarding", body))
                return
            if parsed.path != "/":
                self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<p>Not found.</p>"))
                return
            cookie = self._cookie()
            session = manager.get_session(cookie)
            if not session:
                self._send(HTTPStatus.NOT_FOUND, _page("Link required", "<p>Request a new single-use onboarding link from Puppet Master.</p>"))
                return
            # CSRF plaintext is not stored. The browser gets it only during link exchange,
            # so place a second signed-by-possession value in a non-HttpOnly cookie.
            csrf = secrets.token_urlsafe(24)
            # Rotate the session's CSRF hash for this rendered form.
            def rotate(state: dict[str, Any]) -> None:
                record = state["sessions"].get(_digest(cookie))
                if record and not record.get("used"):
                    record["csrf_hash"] = _digest(csrf)
            manager._mutate(rotate)
            body = (
                "<p>Add an authorized NationStates puppet. Your password is sent only to this HTTPS service, exchanged for X-Autologin, and never stored.</p>"
                f"<form method='post' action='/submit'><input type='hidden' name='csrf' value='{html.escape(csrf)}'>"
                "<label>Puppet nation<input name='nation' required maxlength='80' autocomplete='off'></label>"
                "<label>NationStates password<input name='password' type='password' required maxlength='256' autocomplete='current-password'></label>"
                "<label>Issue policy<select name='decision_profile'>"
                "<option value='manual'>Manual approval</option><option value='preserve'>Preserve puppet direction</option>"
                "<option value='mirror_united_paradisia'>Mirror United Paradisia</option><option value='census_target'>Optimize census statistic</option>"
                "</select></label><label>Census statistic (only for census profile)<input name='census_target' maxlength='100'></label>"
                "<button type='submit'>Add puppet securely</button></form>"
                "<p><small>The link expires in 15 minutes and is invalidated after successful onboarding or three failed attempts.</small></p>"
            )
            self._send(HTTPStatus.OK, _page("Puppet Master onboarding", body))

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/exchange", "/submit"}:
                self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<p>Not found.</p>"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(HTTPStatus.BAD_REQUEST, _page("Invalid request", "<p>The request was rejected.</p>"))
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, _page("Invalid request", "<p>The request was rejected.</p>"))
                return
            try:
                form = parse_qs(self.rfile.read(length).decode("utf-8", errors="strict"), keep_blank_values=True)
            except UnicodeDecodeError:
                self._send(HTTPStatus.BAD_REQUEST, _page("Invalid request", "<p>The request was rejected.</p>"))
                return
            value = lambda key: form.get(key, [""])[0]
            if path == "/exchange":
                # The random 256-bit, single-use token is the capability for this
                # no-password exchange step. Preview crawlers only GET and cannot
                # consume it. The later credential POST still requires cookie,
                # CSRF, and verified same-origin metadata.
                session = manager.exchange_link(value("token"))
                if not session:
                    self._send(HTTPStatus.NOT_FOUND, _page("Link unavailable", "<p>This link is invalid, expired, or already used.</p>"))
                    return
                cookie = f"{COOKIE_NAME}={session['cookie']}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"
                self.send_response(HTTPStatus.SEE_OTHER)
                _security_headers(self)
                self.send_header("Set-Cookie", cookie)
                self.send_header("Location", "/")
                self.end_headers()
                return
            result = manager.submit(
                cookie=self._cookie(),
                csrf=value("csrf"),
                origin=self._verified_origin(),
                nation=value("nation"),
                password=value("password"),
                decision_profile=value("decision_profile"),
                census_target=value("census_target"),
            )
            if result.get("ok"):
                body = _page("Puppet added", f"<p><strong>{html.escape(result['nation'])}</strong> was added with profile <strong>{html.escape(result['decision_profile'])}</strong>.</p><p>You may close this page.</p>")
                self._send(HTTPStatus.OK, body, extra=[("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0")])
            else:
                self._send(HTTPStatus.BAD_REQUEST, _page("Onboarding failed", "<p>The request could not be completed. Check the nation, password, and profile, then retry with this page. After three failures, request a new link.</p>"))

        def _cookie(self) -> str:
            for part in self.headers.get("Cookie", "").split(";"):
                key, sep, value = part.strip().partition("=")
                if sep and key == COOKIE_NAME:
                    return value
            return ""

        def _verified_origin(self) -> str:
            try:
                expected = manager.current_public_url()
            except RuntimeError:
                return ""
            origin = self.headers.get("Origin", "").rstrip("/")
            if origin == expected:
                return expected
            if origin:
                return ""
            expected_parts = urlparse(expected)
            host_matches = self.headers.get("Host", "").lower() == expected_parts.netloc.lower()
            if host_matches and self.headers.get("Sec-Fetch-Site", "") == "same-origin":
                return expected
            referer = urlparse(self.headers.get("Referer", ""))
            same_referer = (
                referer.scheme == expected_parts.scheme
                and referer.netloc == expected_parts.netloc
                and referer.path.startswith("/")
            )
            if host_matches and same_referer:
                return expected
            return ""

    return Handler


def manager_from_env() -> OnboardingManager:
    state_value = os.environ.get("NS_ONBOARD_STATE_FILE", "").strip()
    credentials_value = os.environ.get("NS_CREDENTIALS_FILE", "").strip()
    url_file_value = os.environ.get("NS_ONBOARD_URL_FILE", "")
    if not state_value or not credentials_value:
        raise RuntimeError("onboarding state and credentials paths are required")
    return OnboardingManager(
        state_file=Path(state_value),
        credentials_file=Path(credentials_value),
        user_agent=os.environ.get("NS_USER_AGENT", ""),
        public_url=os.environ.get("NS_ONBOARD_PUBLIC_URL", ""),
        public_url_file=Path(url_file_value) if url_file_value else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Secure Puppet Master HTTPS onboarding")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8871)
    issue = sub.add_parser("issue-link")
    issue.add_argument("--ttl", type=int, default=600)
    args = parser.parse_args()
    manager = manager_from_env()
    if args.command == "issue-link":
        print(manager.issue_link(args.ttl))
        return 0
    server = ThreadingHTTPServer((args.host, args.port), make_handler(manager))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
