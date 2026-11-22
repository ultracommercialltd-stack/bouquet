"""NationStates Puppet Master native tools."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
import urllib.request
import xml.etree.ElementTree as ET

API_URL = "https://www.nationstates.net/cgi-bin/api.cgi"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20

_api_lock = threading.Lock()
_api_blocked_until = 0.0

_MANUAL_ONLY = {
    "move_region",
    "endorse",
    "unendorse",
    "post_rmb",
    "suppress_rmb",
    "unsuppress_rmb",
    "ban",
    "eject",
    "banject",
    "trade_card_html",
    "forum_action",
    "event_action",
}
_DENIED = {"create_nation_html", "answer_issue_html", "dismiss_issue_html", "send_telegram_html"}
_API_AUTONOMOUS = {"public_read", "private_read", "keepalive"}
_POLICY_GATED = {"answer_issue_api", "dismiss_issue_api", "adopt_resolution_api", "gift_card_api"}


def canon(value: str) -> str:
    """Return NationStates' conventional canonical nation key."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", value.strip().lower())).strip("_")


def ns_roster_status(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict) or args:
        return _error("invalid_input", "ns_roster_status accepts no fields.")
    try:
        cfg = _load_credentials()
    except ConfigError as exc:
        return _error(exc.code, str(exc))
    nations = []
    for name, entry in sorted(cfg["nations"].items()):
        nations.append(
            {
                "nation": name,
                "enabled": bool(entry.get("enabled", True)),
                "credential_configured": bool(entry.get("autologin")),
                "allow_issue_autonomy": bool(entry.get("allow_issue_autonomy", False)),
                "decision_profile": entry.get("decision_profile", "manual"),
            }
        )
    return _json({"ok": True, "main_nation": cfg["main_nation"], "nations": nations})


def ns_nation_status(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict) or not isinstance(args.get("nation"), str):
        return _error("invalid_input", "nation is required.")
    nation = canon(args["nation"])
    if not nation:
        return _error("invalid_input", "nation is required.")
    result = _api_request(
        {"nation": nation, "q": "name+region+lastactivity+unstatus"},
        auth=None,
    )
    if not result["ok"]:
        return _json(result)
    try:
        root = ET.fromstring(result["body"])
    except ET.ParseError:
        return _error("bad_response", "NationStates returned malformed XML.")
    text = lambda tag: (root.findtext(tag) or "").strip()
    return _json(
        {
            "ok": True,
            "nation": {
                "name": text("NAME"),
                "canonical": nation,
                "region": text("REGION"),
                "last_activity": text("LASTACTIVITY"),
                "wa_status": text("UNSTATUS"),
            },
            "rate_limit": result["rate_limit"],
        }
    )


def ns_keepalive(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict):
        return _error("invalid_input", "Arguments must be an object.")
    requested = args.get("nations")
    if requested is not None and (
        not isinstance(requested, list) or not all(isinstance(x, str) and canon(x) for x in requested)
    ):
        return _error("invalid_input", "nations must be an array of nation names.")
    try:
        cfg = _load_credentials()
    except ConfigError as exc:
        return _error(exc.code, str(exc))
    roster = cfg["nations"]
    names = [canon(x) for x in requested] if requested is not None else [
        name for name, entry in sorted(roster.items()) if entry.get("enabled", True)
    ]
    unknown = sorted(set(names).difference(roster))
    if unknown:
        return _error("unauthorized_nation", f"Nation is not in the authorized roster: {unknown[0]}.")
    max_actions = _env_int("NS_MAX_ACTIONS_PER_RUN", 20, minimum=1, maximum=100)
    if len(names) > max_actions:
        return _error("action_cap", f"Requested {len(names)} actions; cap is {max_actions}.")
    results = []
    for name in names:
        entry = roster[name]
        if not entry.get("enabled", True):
            results.append({"nation": name, "status": "deferred", "reason": "disabled"})
            continue
        autologin = entry.get("autologin")
        if not isinstance(autologin, str) or not autologin:
            results.append({"nation": name, "status": "failed", "reason": "credential_missing"})
            continue
        response = _api_request({"nation": name, "q": "ping"}, auth=("X-Autologin", autologin))
        if response["ok"] and "<PING>1</PING>" in response["body"]:
            results.append({"nation": name, "status": "passed", "http_status": response["status"]})
        elif response.get("status") == 409:
            results.append({"nation": name, "status": "passed", "reason": "recent_login", "http_status": 409})
        elif response.get("status") == 429:
            results.append({"nation": name, "status": "deferred", "reason": "rate_limited", "http_status": 429})
            break
        else:
            results.append(
                {
                    "nation": name,
                    "status": "failed",
                    "reason": response.get("error", {}).get("code", "api_failure"),
                    "http_status": response.get("status", 0),
                }
            )
    ok = bool(results) and all(row["status"] in {"passed", "deferred"} for row in results)
    return _json({"ok": ok, "results": results})


def ns_get_issues(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict) or not isinstance(args.get("nation"), str):
        return _error("invalid_input", "nation is required.")
    nation = canon(args["nation"])
    try:
        cfg = _load_credentials()
    except ConfigError as exc:
        return _error(exc.code, str(exc))
    entry = cfg["nations"].get(nation)
    if not entry:
        return _error("unauthorized_nation", "Nation is not in the authorized roster.")
    autologin = entry.get("autologin")
    if not isinstance(autologin, str) or not autologin:
        return _error("credential_missing", "Nation credential is not configured.")
    response = _api_request({"nation": nation, "q": "issues"}, auth=("X-Autologin", autologin))
    if not response["ok"]:
        return _json(response)
    try:
        root = ET.fromstring(response["body"])
    except ET.ParseError:
        return _error("bad_response", "NationStates returned malformed issue XML.")
    issues = []
    for issue in root.findall(".//ISSUE"):
        try:
            issue_id = int(issue.get("id", ""))
        except ValueError:
            continue
        options = []
        for option in issue.findall("OPTION"):
            try:
                option_id = int(option.get("id", ""))
            except ValueError:
                continue
            options.append({"id": option_id, "text": (option.text or "").strip()})
        issues.append(
            {
                "id": issue_id,
                "title": (issue.findtext("TITLE") or "").strip(),
                "text": (issue.findtext("TEXT") or "").strip(),
                "options": options,
            }
        )
    return _json(
        {
            "ok": True,
            "nation": nation,
            "decision_profile": entry.get("decision_profile", "manual"),
            "issues": issues,
            "rate_limit": response["rate_limit"],
        }
    )


def ns_answer_issue(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict):
        return _error("invalid_input", "Arguments must be an object.")
    nation = canon(str(args.get("nation", "")))
    if not nation or not isinstance(args.get("issue_id"), int) or not isinstance(args.get("option_id"), int):
        return _error("invalid_input", "nation, integer issue_id, and integer option_id are required.")
    if not _env_bool("NS_ALLOW_AUTONOMOUS_ISSUES", False):
        return _error("policy_denied", "Autonomous issue actions are disabled by operator policy.")
    try:
        cfg = _load_credentials()
    except ConfigError as exc:
        return _error(exc.code, str(exc))
    entry = cfg["nations"].get(nation)
    if not entry:
        return _error("unauthorized_nation", "Nation is not in the authorized roster.")
    if nation == cfg["main_nation"]:
        return _error("main_nation_manual", "The main nation always requires manual issue approval.")
    if not entry.get("enabled", True):
        return _error("policy_denied", "This nation is disabled.")
    if not entry.get("allow_issue_autonomy", False):
        return _error("policy_denied", "This nation is not enabled for autonomous issue actions.")
    profile = entry.get("decision_profile", "manual")
    if profile not in {"preserve", "mirror_united_paradisia", "census_target"}:
        return _error("policy_denied", "This nation has no valid autonomous decision profile.")
    autologin = entry.get("autologin")
    if not isinstance(autologin, str) or not autologin:
        return _error("credential_missing", "Nation credential is not configured.")
    response = _api_request(
        {},
        auth=("X-Autologin", autologin),
        data={
            "nation": nation,
            "c": "issue",
            "issue": str(args["issue_id"]),
            "option": str(args["option_id"]),
        },
    )
    if not response["ok"]:
        return _json(response)
    try:
        root = ET.fromstring(response["body"])
    except ET.ParseError:
        return _error("bad_response", "NationStates returned malformed issue-result XML.")
    error_text = (root.findtext(".//ERROR") or "").strip()
    if error_text:
        return _error("issue_rejected", error_text)
    return _json(
        {
            "ok": True,
            "status": "passed",
            "nation": nation,
            "issue_id": args["issue_id"],
            "option_id": args["option_id"],
            "decision_profile": profile,
            "description": (root.findtext(".//DESC") or "").strip(),
        }
    )


def ns_action_policy(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict) or not isinstance(args.get("action"), str):
        return _error("invalid_input", "action is required.")
    action = canon(args["action"])
    if action in _DENIED:
        return _json({"ok": True, "action": action, "classification": "denied", "executor": None})
    if action in _MANUAL_ONLY:
        return _json(
            {"ok": True, "action": action, "classification": "manual_only", "executor": "Bouquet"}
        )
    if action in _API_AUTONOMOUS:
        return _json(
            {"ok": True, "action": action, "classification": "autonomous", "executor": "NationStates API"}
        )
    if action in _POLICY_GATED:
        return _json(
            {"ok": True, "action": action, "classification": "policy_gated", "executor": "NationStates API"}
        )
    return _json({"ok": True, "action": action, "classification": "unknown", "executor": None})


class ConfigError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _load_credentials() -> dict[str, Any]:
    path = Path(os.environ.get("NS_CREDENTIALS_FILE", "")).expanduser()
    if not str(path) or not path.is_file():
        raise ConfigError("configuration_error", "NationStates credentials are not configured.")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError("insecure_permissions", "NationStates credentials file must have mode 0600.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("configuration_error", "NationStates credentials file is unreadable.") from exc
    raw_nations = data.get("nations") if isinstance(data, dict) else None
    if not isinstance(raw_nations, dict):
        raise ConfigError("configuration_error", "NationStates roster is invalid.")
    nations = {}
    for raw_name, raw_entry in raw_nations.items():
        name = canon(str(raw_name))
        if name and isinstance(raw_entry, dict):
            nations[name] = dict(raw_entry)
    main = canon(str(data.get("main_nation") or os.environ.get("NS_MAIN_NATION", "")))
    return {"version": data.get("version", 1), "main_nation": main, "nations": nations}


def _api_request(
    params: dict[str, str],
    auth: tuple[str, str] | None,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    global _api_blocked_until
    user_agent = os.environ.get("NS_USER_AGENT", "").strip()
    if not user_agent:
        return _error_obj("configuration_error", "NS_USER_AGENT is required.")
    query = urlencode(params)
    url = f"{API_URL}?{query}" if query else API_URL
    encoded = urlencode(data).encode("utf-8") if data is not None else None
    request = Request(url, data=encoded, headers={"User-Agent": user_agent})
    if auth:
        request.add_header(auth[0], auth[1])
    with _api_lock:
        delay = _api_blocked_until - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                rate = _rate_headers(response.headers)
                _update_rate_limit(response.status, rate)
                if len(body) > MAX_RESPONSE_BYTES:
                    return _error_obj("bad_response", "NationStates response was too large.", status=response.status)
                return {
                    "ok": True,
                    "status": response.status,
                    "body": body.decode("utf-8", errors="replace"),
                    "rate_limit": rate,
                }
        except HTTPError as exc:
            rate = _rate_headers(exc.headers)
            _update_rate_limit(exc.code, rate)
            return _error_obj(
                "http_error",
                "NationStates returned an HTTP error.",
                status=exc.code,
                rate_limit=rate,
            )
        except (URLError, TimeoutError, OSError):
            return _error_obj("network_error", "NationStates request failed.", status=0)


def _rate_headers(headers: Any) -> dict[str, int | None]:
    def number(name: str) -> int | None:
        try:
            value = headers.get(name)
            return int(value) if value is not None else None
        except (AttributeError, TypeError, ValueError):
            return None

    return {
        "limit": number("RateLimit-Limit"),
        "remaining": number("RateLimit-Remaining"),
        "reset_seconds": number("RateLimit-Reset"),
        "retry_after_seconds": number("Retry-After"),
    }


def _update_rate_limit(status: int, rate: dict[str, int | None]) -> None:
    global _api_blocked_until
    remaining = rate.get("remaining")
    if status == 429 or (remaining is not None and remaining <= 1):
        seconds = rate.get("retry_after_seconds") or rate.get("reset_seconds") or 1
        _api_blocked_until = max(_api_blocked_until, time.monotonic() + int(seconds) + 1)


def reset_rate_limiter_for_tests() -> None:
    global _api_blocked_until
    _api_blocked_until = 0.0


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _error(code: str, message: str, **extra: Any) -> str:
    return _json(_error_obj(code, message, **extra))


def _error_obj(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    payload.update(extra)
    return payload
