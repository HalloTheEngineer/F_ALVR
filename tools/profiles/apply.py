#!/usr/bin/env python3
"""Apply/revert ALVR setting profiles (e.g. the Beat Saber "ranked" profile) through the ALVR
web API.

Usage:
    apply.py <profile_name> [--restart] [--no-backup]
    apply.py revert
    apply.py status

- <profile_name> refers to tools/profiles/<name>.json or tools/profiles/experiments/<name>.json
- On Windows, run this from the ALVR streamer folder (session.json sits next to ALVR Dashboard.exe)
  or pass --config <path to session.json>
- The first apply of a profile stores the previous values in
  <config_dir>/profile_backup_<name>.json, used by `revert`
- --restart issues a SteamVR restart afterwards (needed for steamvr-restart flagged settings
  like fps/codec)
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROFILES_DIR = Path(__file__).parent
CONFIG_PATH = None


def resolve_config_path(explicit):
    if explicit:
        return Path(explicit)

    candidates = []

    env_dir = os.environ.get("ALVR_CONFIG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / "session.json")

    # Windows installs (portable) keep session.json next to ALVR Dashboard.exe, so running the
    # script from the streamer folder is the primary case
    candidates.append(Path.cwd() / "session.json")

    # Next to the script or in one of its parent folders
    here = Path(__file__).resolve().parent
    for _ in range(5):
        candidates.append(here / "session.json")
        here = here.parent

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "alvr" / "session.json")
    else:
        candidates.append(Path.home() / ".config" / "alvr" / "session.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    sys.exit("session.json not found; run from the ALVR streamer folder or pass --config <path>")


def resolve_profile(name):
    for candidate in (
        PROFILES_DIR / f"{name}.json",
        PROFILES_DIR / "experiments" / f"{name}.json",
    ):
        if candidate.exists():
            return candidate
    sys.exit(f"Profile not found: {name}")


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


def wait_for_server(port, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            api_post(port, "ping")
            return True
        except Exception:
            time.sleep(1)
    return False


def find_windows_vrmonitor():
    candidates = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path = Path(winreg.QueryValueEx(key, "SteamPath")[0])
            candidates.append(
                steam_path / "steamapps/common/SteamVR/bin/win64/vrmonitor.exe"
            )
    except OSError:
        pass

    for base in (
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam",
        r"E:\Steam",
    ):
        candidates.append(
            Path(base) / "steamapps/common/SteamVR/bin/win64/vrmonitor.exe"
        )

    return next((candidate for candidate in candidates if candidate.exists()), None)


def restart_steamvr(port):
    # NOTE: the /api/steamvr/restart endpoint restarts SteamVR without rescanning external
    # drivers (the ALVR driver does not load again). A full process restart is required.
    print("Restarting SteamVR (full process restart)...")

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "vrserver.exe", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/IM", "vrmonitor.exe", "/F"], capture_output=True)
        time.sleep(4)

        vrmonitor = find_windows_vrmonitor()
        if vrmonitor is not None:
            subprocess.Popen([str(vrmonitor)], close_fds=True)
        else:
            print("WARNING: vrmonitor.exe not found, requesting SteamVR via Steam")
            os.startfile("steam://rungameid/250820")  # noqa: S606

        wait_for_server(port, timeout_s=60)
        print("SteamVR restarted")
        return

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
    profile_path = resolve_profile(profile_name)
    profile = json.loads(profile_path.read_text())
    session = load_session()
    port = api_port(session)

    safe_name = profile_name.replace("/", "_")
    backup_path = CONFIG_PATH.parent / f"profile_backup_{safe_name}.json"
    if backup and not backup_path.exists():
        original_values = [
            {"path": desc["path"], "value": get_value(session, desc["path"])}
            for desc in profile["values"]
        ]
        backup_path.write_text(
            json.dumps({"profile": profile_name, "values": original_values}, indent=1)
        )
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

    try:
        status = api_post(port, "session/values", payload)
    except (urllib.error.URLError, ConnectionError) as e:
        print(f"ERROR: ALVR server not reachable on port {port} ({e}).")
        print("Start ALVR Dashboard and SteamVR, then try again.")
        return False
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

    return not mismatches


def revert(profile_name):
    safe_name = profile_name.replace("/", "_")
    backup_path = CONFIG_PATH.parent / f"profile_backup_{safe_name}.json"
    if not backup_path.exists():
        sys.exit(f"No backup found for profile '{profile_name}'")

    backup = json.loads(backup_path.read_text())
    session = load_session()
    port = api_port(session)

    payload = [
        {"path": [{"Name": segment} for segment in desc["path"]], "value": desc["value"]}
        for desc in backup["values"]
    ]
    try:
        status = api_post(port, "session/values", payload)
    except (urllib.error.URLError, ConnectionError) as e:
        sys.exit(f"ERROR: ALVR server not reachable on port {port} ({e})")
    print(f"Reverted {len(payload)} values ({profile_name}) -> HTTP {status}")

    profile_path = resolve_profile(profile_name)
    profile = json.loads(profile_path.read_text())
    if profile.get("steamvr_restart"):
        restart_steamvr(port)

    backup_path.unlink()
    print("Backup removed")

    return True


def beep(times=1):
    if sys.platform != "win32":
        return
    try:
        import winsound

        for _ in range(times):
            winsound.Beep(880, 180)
            time.sleep(0.05)
    except Exception:
        pass


def unix_ms():
    return int(time.time() * 1000)


def sequence(schedule_path, out_dir=None):
    schedule = json.loads(Path(schedule_path).read_text())
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path(out_dir) if out_dir else Path.cwd() / "captures" / stamp
    out.mkdir(parents=True, exist_ok=True)
    schedule_log_path = out / "schedule.json"

    log = {
        "schedule": str(schedule_path),
        "started_unix_ms": unix_ms(),
        "stages": [],
    }

    def write_log():
        schedule_log_path.write_text(json.dumps(log, indent=1))

    print(f"Session output: {out}")
    print(f"Stages: {len(schedule['stages'])}")
    print("Keep the headset on and play the fixed map continuously.")

    aborted = False
    for index, stage in enumerate(schedule["stages"]):
        label = stage.get("label", stage.get("profile") or f"stage{index + 1}")
        profile = stage.get("profile")
        duration = int(stage["duration_s"])
        record = {
            "index": index,
            "label": label,
            "profile": profile,
            "duration_s": duration,
            "apply_unix_ms": unix_ms(),
        }

        if profile:
            profile_data = json.loads(resolve_profile(profile).read_text())
            if profile_data.get("steamvr_restart") and not stage.get("allow_restart"):
                record["error"] = "profile requires a SteamVR restart; use Session B manually"
                log["stages"].append(record)
                write_log()
                print(f"ERROR: {record['error']}")
                aborted = True
                break

            print(f"\n=== Stage {index + 1}/{len(schedule['stages'])}: {label} ({profile}) ===")
            beep(2)
            if not apply(profile, do_restart=False):
                record["error"] = "values did not stick"
                log["stages"].append(record)
                write_log()
                print("ERROR: stage aborted because values did not stick")
                aborted = True
                break
        else:
            print(f"\n=== Stage {index + 1}/{len(schedule['stages'])}: {label} ===")
            beep(2)

        print(f"Play for {duration} seconds...")
        time.sleep(duration)

        record["end_unix_ms"] = unix_ms()
        if profile and not stage.get("keep"):
            revert(profile)
        beep(1)

        log["stages"].append(record)
        write_log()

    log["finished_unix_ms"] = unix_ms()
    write_log()

    print(f"\nSequence {'aborted' if aborted else 'finished'}. Schedule: {schedule_log_path}")
    print("Run collect.ps1 to save the telemetry.")


def status():
    session = load_session()
    port = api_port(session)
    print(f"session.json: {CONFIG_PATH}")
    print(f"API port: {port}")
    for backup in sorted(CONFIG_PATH.parent.glob("profile_backup_*.json")):
        print(f"Active backup: {backup.name}")
    for profile in sorted(PROFILES_DIR.glob("*.json")):
        print(f"Profile: {profile.stem}")
    for profile in sorted((PROFILES_DIR / "experiments").glob("*.json")):
        print(f"Profile: experiments/{profile.stem}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("profile", nargs="?", default=None)
    parser.add_argument("--restart", action="store_true", default=True)
    parser.add_argument("--no-restart", dest="restart", action="store_false")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--config", default=None, help="path to session.json")
    parser.add_argument("--out", default=None, help="sequence output directory")
    args = parser.parse_args()

    global CONFIG_PATH
    CONFIG_PATH = resolve_config_path(args.config)

    if args.command == "status":
        status()
    elif args.command == "revert":
        revert(args.profile or "ranked")
    elif args.command == "apply":
        if not args.profile:
            sys.exit("apply requires a profile name")
        apply(args.profile, args.restart, not args.no_backup)
    elif args.command == "sequence":
        if not args.profile:
            sys.exit("sequence requires a schedule file")
        sequence(args.profile, args.out)
    else:
        sys.exit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
