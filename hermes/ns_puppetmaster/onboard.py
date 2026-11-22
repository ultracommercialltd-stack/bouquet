"""Secure local onboarding for NationStates puppet credentials.

The password is accepted only in process memory, exchanged for X-Autologin through
the official API ping shard, and never written to disk.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request
import urllib.request

from .tools import API_URL, REQUEST_TIMEOUT_SECONDS, canon


def add_nation(
    credentials_file: Path,
    nation: str,
    password: str,
    *,
    user_agent: str,
    allow_issue_autonomy: bool = False,
    decision_profile: str = "manual",
    census_target: str | None = None,
) -> dict[str, str]:
    name = canon(nation)
    if not name:
        raise ValueError("nation is required")
    if not password:
        raise ValueError("password is required")
    if not user_agent.strip():
        raise ValueError("user_agent is required")
    allowed_profiles = {"manual", "preserve", "mirror_united_paradisia", "census_target"}
    if decision_profile not in allowed_profiles:
        raise ValueError("invalid decision profile")
    if allow_issue_autonomy and decision_profile == "manual":
        raise ValueError("autonomous issue answering requires a non-manual decision profile")
    if decision_profile == "census_target" and not (census_target or "").strip():
        raise ValueError("census_target profile requires a census target")

    url = f"{API_URL}?{urlencode({'nation': name, 'q': 'ping'})}"
    request = Request(url, headers={"User-Agent": user_agent, "X-Password": password})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        autologin = response.headers.get("X-Autologin", "").strip()
    if response.status != 200 or "<PING>1</PING>" not in body:
        raise RuntimeError(f"NationStates rejected the credential (HTTP {response.status}).")
    if not autologin:
        raise RuntimeError("NationStates did not return X-Autologin.")

    if credentials_file.exists():
        data = json.loads(credentials_file.read_text(encoding="utf-8"))
    else:
        data = {"version": 1, "main_nation": name, "nations": {}}
    data.setdefault("version", 1)
    data.setdefault("main_nation", name)
    nations = data.setdefault("nations", {})
    previous = nations.get(name) if isinstance(nations.get(name), dict) else {}
    nations[name] = {
        "enabled": bool(previous.get("enabled", True)),
        "autologin": autologin,
        "allow_issue_autonomy": allow_issue_autonomy,
        "decision_profile": decision_profile,
    }
    if census_target:
        nations[name]["census_target"] = census_target.strip()
    _atomic_write_credentials(credentials_file, data)
    return {"nation": name, "status": "added"}


def _atomic_write_credentials(credentials_file: Path, data: dict) -> None:
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = credentials_file.with_suffix(credentials_file.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.replace(tmp, credentials_file)
    os.chmod(credentials_file, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Add a NationStates puppet without storing its password.")
    parser.add_argument("nation", help="Nation name")
    parser.add_argument(
        "--credentials-file",
        default=os.environ.get("NS_CREDENTIALS_FILE", ""),
        help="Mode-0600 roster path (defaults to NS_CREDENTIALS_FILE)",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("NS_USER_AGENT", ""),
        help="NationStates API User-Agent (defaults to NS_USER_AGENT)",
    )
    parser.add_argument(
        "--decision-profile",
        choices=["manual", "preserve", "mirror_united_paradisia", "census_target"],
        default="manual",
    )
    parser.add_argument("--autonomous-issues", action="store_true")
    parser.add_argument("--census-target", help="World Census statistic for census_target profile")
    args = parser.parse_args()
    if not args.credentials_file:
        parser.error("--credentials-file or NS_CREDENTIALS_FILE is required")
    password = getpass.getpass(f"NationStates password for {args.nation}: ")
    result = add_nation(
        Path(args.credentials_file),
        args.nation,
        password,
        user_agent=args.user_agent,
        allow_issue_autonomy=args.autonomous_issues,
        decision_profile=args.decision_profile,
        census_target=args.census_target,
    )
    print(f"Added {result['nation']} using X-Autologin; password was not stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
