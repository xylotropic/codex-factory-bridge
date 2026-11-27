#!/usr/bin/env python3
"""Install, inspect, launch, or remove the local Codex Factory bridge on macOS."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
STATE = HOME / "Library/Application Support/Codex Factory Bridge"
AGENTS = HOME / "Library/LaunchAgents"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex")).expanduser().resolve()
APP = Path(os.environ.get("FACTORY_CODEX_APP", "/Applications/ChatGPT.app")).expanduser()
BINARY = APP / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
DROID = Path(os.environ.get("FACTORY_DROID") or shutil.which("droid")
             or HOME / ".local/bin/droid").expanduser().resolve()
LABEL = "local.codex-factory-bridge"
GATEWAY_PORT = 17382
BRIDGE_PORT = 17383
DOMAIN = f"gui/{os.getuid()}"


def run(*args, check=True, quiet=False):
    return subprocess.run([str(arg) for arg in args], check=check, text=True,
                          capture_output=quiet)


def write_private(path, data):
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def uri(port, path):
    return f"http://127.0.0.1:{port}{path}"


def get(port, path):
    with urlopen(uri(port, path), timeout=4) as response:
        return json.load(response)


def launch_args():
    python = Path(sys.executable).resolve()
    return {
        LABEL + ".gateway": [python, ROOT / "gateway.py", "--port", GATEWAY_PORT, "--state", STATE],
        LABEL + ".desktop": [python, ROOT / "bridge.py", "--port", BRIDGE_PORT,
                              "--gateway", GATEWAY_PORT, "--state", STATE, "--home", CODEX_HOME],
    }


def agent_file(label):
    return AGENTS / f"{label}.plist"


def loaded(label):
    return run("/bin/launchctl", "print", f"{DOMAIN}/{label}", check=False, quiet=True).returncode == 0


def check_prerequisites():
    if sys.platform != "darwin" or sys.version_info < (3, 14):
        raise RuntimeError("Use macOS with Python 3.14 or newer.")
    if not os.access(DROID, os.X_OK):
        raise RuntimeError("Install Factory Droid and sign in to the CLI first.")
    if not os.access(BINARY, os.X_OK):
        raise RuntimeError(f"The official Codex CLI could not be found in {APP}.")
    # The launcher runs the bundled binary directly, preserving its signing identity.
    if run("/usr/bin/codesign", "--verify", "--strict", BINARY, check=False, quiet=True).returncode:
        raise RuntimeError("The bundled Codex CLI did not pass its macOS signature check.")


def install():
    check_prerequisites()
    for port, label in ((GATEWAY_PORT, LABEL + ".gateway"), (BRIDGE_PORT, LABEL + ".desktop")):
        if not loaded(label):
            # A different local service must be moved or stopped by its owner.
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"Port {port} is already in use. Existing services are left intact.")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if STATE.stat().st_mode & 0o077:
        raise RuntimeError(f"Restrict access to the local state directory with chmod 700 {STATE}.")
    AGENTS.mkdir(parents=True, exist_ok=True)
    environment = {"FACTORY_DROID": str(DROID)}
    if "FACTORY_LIMITS_DIR" in os.environ:
        environment["FACTORY_LIMITS_DIR"] = str(Path(os.environ["FACTORY_LIMITS_DIR"]).expanduser().resolve())
    for label, args in launch_args().items():
        config = {
            "Label": label,
            "ProgramArguments": list(map(str, args)),
            "WorkingDirectory": str(ROOT),
            "EnvironmentVariables": environment,
            "KeepAlive": True, "ThrottleInterval": 10,
            "StandardOutPath": str(STATE / f"{label}.log"),
            "StandardErrorPath": str(STATE / f"{label}.log"),
        }
        target = agent_file(label)
        payload = plistlib.dumps(config)
        if target.exists() and target.read_bytes() != payload:
            raise RuntimeError(f"Another installation owns {target}. Uninstall it before reinstalling.")
        write_private(target, payload)
        if not loaded(label):
            run("/bin/launchctl", "bootstrap", DOMAIN, target)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if get(BRIDGE_PORT, "/health")["status"] == "ready":
                print("Installed. Quit ChatGPT/Codex, then run: python3.14 setup.py launch")
                return
        except (URLError, OSError):
            time.sleep(.4)
    raise RuntimeError("The desktop bridge did not start. Inspect the logs in " + str(STATE))


def launch(thread_url=None):
    check_prerequisites()
    executable = APP / "Contents/MacOS" / APP.stem
    pattern = "^" + re.escape(str(executable)) + r"([[:space:]]|$)"
    if run("/usr/bin/pgrep", "-f", pattern, check=False, quiet=True).returncode == 0:
        raise RuntimeError("Quit the running ChatGPT/Codex app before launching the bridge.")
    if get(BRIDGE_PORT, "/health")["status"] != "ready":
        raise RuntimeError("Install and start the desktop bridge first.")
    if thread_url:
        target = urlsplit(thread_url)
        if target.scheme != "codex" or target.netloc != "threads":
            raise RuntimeError("Use a codex://threads/ link from the Codex app.")
    args = ["/usr/bin/open", "-a", APP, "--env",
            f"CODEX_APP_SERVER_OPENAI_BASE_URL={uri(BRIDGE_PORT, '/v1')}"]
    run(*args, *([thread_url] if thread_url else []))


def status():
    bridge = get(BRIDGE_PORT, "/health")
    print(json.dumps(bridge, indent=2))
    try:
        quota = get(GATEWAY_PORT, "/quota")
    except (URLError, OSError) as error:
        raise RuntimeError("Factory limits are unavailable. Check Droid sign-in.") from error
    print(json.dumps(quota, indent=2))


def uninstall():
    for label in reversed(tuple(launch_args())):
        if loaded(label):
            run("/bin/launchctl", "bootout", f"{DOMAIN}/{label}")
        agent_file(label).unlink(missing_ok=True)
    print("Bridge services removed. Open the standard app from Applications. Local data is at " + str(STATE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "launch", "status", "uninstall"))
    parser.add_argument("--thread", help="Existing local thread link to open on launch")
    args = parser.parse_args()
    try:
        if args.action == "launch":
            launch(args.thread)
        else:
            {"install": install, "status": status, "uninstall": uninstall}[args.action]()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
