#!/usr/bin/env python3
"""Apply/revert ALVR setting profiles (e.g. the Beat Saber "ranked" profile) through the ALVR
web API.

Usage:
    apply.py <profile_name> [--restart] [--no-backup]
    apply.py revert
    apply.py status

- <profile_name> refers to tools/profiles/<name>.json
- The first apply of a profile stores the previous values in
  <config_dir>/profile_backup_<name>.json, used by `revert`
- --restart issues a SteamVR restart afterwards (needed for steamvr-restart flagged settings
  like fps/codec)
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "alvr" / "session.json"
PROFILES_DIR = Path(__file__).parent


def load_session():
    return json.loads(CONFIG_PATH.read_text())


def api_port(session):
    return session["session_settings"]["connection"]["web_server_port"]


def api_post(port, endpoint, payload=None):
    url = f"http://127.0.0.1:{port}/api/{endpoint}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else b"",
        headers={"Content-Type": "application/json", "X-ALVR": "true"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def get_value(session, path):
    ref = session
    for segment in path:
        ref = ref[segment]
    return ref


def set_value(session, path, value):
    ref = session["session_settings"]
    for segment in path[:-1]:
        ref = ref[segment]
    ref[path[-1]] = value


def wait_for_server(port, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            api_post(port, "ping")
            return True
        except Exception:
            time.sleep(1)
    return False


def restart_steamvr(port):
    # NOTE: the /api/steamvr/restart endpoint restarts SteamVR without rescanning external
    # drivers (the ALVR driver does not load again). A full process restart is required on Linux.
    import subprocess

    print("Restarting SteamVR (full process restart)...")
    subprocess.run(["pkill", "-f", "vrmonitor"], check=False)
    subprocess.run(["pkill", "-f", "vrserver"], check=False)
    time.sleep(4)

    vrmonitor = (
        Path.home()
        / ".local/share/Steam/steamapps/common/SteamVR/bin/vrmonitor.sh"
    )
    if vrmonitor.exists():
        subprocess.Popen(
            [str(vrmonitor)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        print(f"WARNING: {vrmonitor} not found, start SteamVR manually")

    wait_for_server(port, timeout_s=45)
    print("SteamVR restarted")


def apply(profile_name, do_restart, backup=True):
    profile_path = PROFILES_DIR / f"{profile_name}.json"
    profile = json.loads(profile_path.read_text())
    session = load_session()
    port = api_port(session)

    backup_path = CONFIG_PATH.parent / f"profile_backup_{profile_name}.json"
    if backup and not backup_path.exists():
        original_values = [
            {"path": desc["path"], "value": get_value(session, desc["path"])}
            for desc in profile["values"]
        ]
        backup_path.write_text(json.dumps({"profile": profile_name, "values": original_values}, indent=1))
        print(f"Backup written: {backup_path}")
    elif backup_path.exists():
        print(f"Backup already exists, keeping it: {backup_path}")

    payload = [
        {
            "path": [{"Name": segment} for segment in desc["path"]],
            "value": desc["value"],
        }
        for desc in profile["values"]
    ]

    status = api_post(port, "session/values", payload)
    print(f"Applied {len(payload)} values ({profile_name}) -> HTTP {status}")

    # verify against the persisted session
    time.sleep(0.5)
    fresh = load_session()
    mismatches = []
    for desc in profile["values"]:
        actual = get_value(fresh, desc["path"])
        if actual != desc["value"]:
            mismatches.append((desc["path"], desc["value"], actual))

    if mismatches:
        print(f"WARNING: {len(mismatches)} values did not stick:")
        for path, expected, actual in mismatches:
            print(f"  {'.'.join(path)}: expected {expected!r}, got {actual!r}")
    else:
        print("All values verified in session.json")

    if do_restart and profile.get("steamvr_restart"):
        restart_steamvr(port)


def revert(profile_name):
    backup_path = CONFIG_PATH.parent / f"profile_backup_{profile_name}.json"
    if not backup_path.exists():
        sys.exit(f"No backup found for profile '{profile_name}'")

    backup = json.loads(backup_path.read_text())
    session = load_session()
    port = api_port(session)

    payload = [
        {"path": [{"Name": segment} for segment in desc["path"]], "value": desc["value"]}
        for desc in backup["values"]
    ]
    status = api_post(port, "session/values", payload)
    print(f"Reverted {len(payload)} values ({profile_name}) -> HTTP {status}")

    if (PROFILES_DIR / f"{profile_name}.json").exists():
        profile = json.loads((PROFILES_DIR / f"{profile_name}.json").read_text())
        if profile.get("steamvr_restart"):
            restart_steamvr(port)

    backup_path.unlink()
    print("Backup removed")


def status():
    session = load_session()
    port = api_port(session)
    print(f"session.json: {CONFIG_PATH}")
    print(f"API port: {port}")
    for backup in sorted(CONFIG_PATH.parent.glob("profile_backup_*.json")):
        print(f"Active backup: {backup.name}")
    for profile in sorted(PROFILES_DIR.glob("*.json")):
        print(f"Profile: {profile.stem}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("profile", nargs="?", default=None)
    parser.add_argument("--restart", action="store_true", default=True)
    parser.add_argument("--no-restart", dest="restart", action="store_false")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if args.command == "status":
        status()
    elif args.command == "revert":
        revert(args.profile or "ranked")
    elif args.command == "apply":
        if not args.profile:
            sys.exit("apply requires a profile name")
        apply(args.profile, args.restart, not args.no_backup)
    else:
        sys.exit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
