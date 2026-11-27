"""Factory session and Standard allowance, using Droid's signed-in local CLI."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time
from datetime import datetime, timezone

DROID = Path(os.environ.get("FACTORY_DROID") or shutil.which("droid") or
             Path.home() / ".local/bin/droid").expanduser().resolve()
ESCAPE = re.compile(rb"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[78DEHM])")
WINDOWS = {"5-hour": ("5 hours", 300), "Weekly": ("7 days", 10080),
           "Monthly": ("30 days", 43200)}
CREDENTIALS = ("FACTORY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
               "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY", "GEMINI_API_KEY")


class FactoryError(RuntimeError):
    pass


def environment():
    return {key: value for key, value in os.environ.items() if key not in CREDENTIALS}


def parse_allowance(output):
    text = ESCAPE.sub(b"", output).decode(errors="replace")
    for panel in reversed(text.split("Usage & Limits")[1:]):
        if "◉ Standard" not in panel or "When limit is reached" not in panel:
            continue
        rows = {name: (int(used), reset.strip()) for name, used, reset in re.findall(
            r"│\s*(5-hour|Weekly|Monthly)\s+(\d{1,3})\s*%[^│\r\n]*?↻\s*([^│\r\n]+)│",
            panel)}
        balance = re.search(r"\(\$([\d,.]+) remaining\)", panel)
        preference = re.search(r"│\s*>\s*(Switch to Droid Core|Enable Extra Usage in settings)", panel)
        if not (balance and preference and all(
                0 <= rows.get(name, (-1, None))[0] <= 100 for name in WINDOWS)):
            continue
        return {
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "windows": [{"label": label, "windowMinutes": minutes,
                         "usedPercent": rows[name][0], "resetsIn": rows[name][1]}
                        for name, (label, minutes) in WINDOWS.items()],
            "extraUsageBalance": balance[1],
            "overagePreference": "droidCore" if preference[1].startswith("Switch") else "extraUsage",
        }
    return None


def read_allowance(directory):
    master, terminal = pty.openpty()
    process = None
    try:
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 55, 125, 0, 0))
        process = subprocess.Popen(
            [str(DROID), "--disable-builtin-skills"], cwd=directory,
            stdin=terminal, stdout=terminal, stderr=terminal,
            env={**environment(), "TERM": "dumb", "NO_COLOR": "1", "LANG": "en_US.UTF-8"},
        )
        os.close(terminal)
        terminal = None
        output, sent = b"", False
        deadline = time.monotonic() + 12
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([master], [], [], remaining)[0]:
                break
            try:
                chunk = os.read(master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            output = (output + chunk)[-262144:]
            if b"Trust this folder?" in output:
                raise FactoryError(f"Open Droid in {directory} and approve this folder before reading limits.")
            if not sent and "│ > ".encode() in output and b"? for help" in output:
                os.write(master, b"/limits\r")
                sent = True
            if sent and (allowance := parse_allowance(output)):
                return allowance
    finally:
        os.close(master)
        if terminal is not None:
            os.close(terminal)
        if process:
            stop(process)
    raise FactoryError("Droid could not read the signed-in Factory Standard limits.")


def require_standard(allowance):
    if allowance["overagePreference"] != "droidCore" or allowance["extraUsageBalance"] != "0.00":
        raise FactoryError("Factory must show Droid Core at exhaustion and a $0.00 Extra Usage balance.")
    if any(window["usedPercent"] >= 100 for window in allowance["windows"]):
        raise FactoryError("A Factory Standard limit is exhausted.")


def read_models(directory):
    """Discover exact models through Droid's local JSON-RPC session transport."""
    request = {
        "factoryApiVersion": "1.0.0", "factoryProtocolVersion": "1.0.0",
        "type": "request", "jsonrpc": "2.0", "id": "codex-factory-models",
        "method": "droid.list_models", "params": {"includeDisabled": True},
    }
    command = [str(DROID), "exec", "--cwd", str(directory),
               "--input-format", "stream-jsonrpc", "--output-format", "stream-jsonrpc"]
    result = subprocess.run(command, input=json.dumps(request) + "\n", env=environment(),
                            capture_output=True, text=True, timeout=12, check=False)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            response = json.loads(line)
            if response.get("id") == request["id"] and isinstance(response.get("result"), dict):
                models = response["result"].get("models")
                if isinstance(models, list) and models:
                    return models
    raise FactoryError("Droid could not verify the signed-in Factory model catalog.")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()



def verified_session(directory, session_id, model, effort):
    """Read Droid's recorded assistant attribution and final text for this turn."""
    if not re.fullmatch(r"[0-9a-f-]{36}", session_id or ""):
        raise FactoryError("Droid did not return a session ID.")
    scope = str(directory.resolve()).replace("/", "-")
    path = Path.home() / ".factory/sessions" / scope / f"{session_id}.jsonl"
    try:
        with path.open(encoding="utf-8") as transcript:
            messages = [json.loads(line).get("message", {}) for line in transcript]
    except (OSError, ValueError) as exc:
        raise FactoryError("Droid's model attribution could not be read.") from exc
    attributed = [message for message in messages if message.get("role") == "assistant"
                  and message.get("modelId")]
    if not attributed or any(message["modelId"] != model or message.get("reasoningEffort") != effort
                             for message in attributed):
        raise FactoryError("Droid reported a different effective model or reasoning level.")
    final = attributed[-1]
    text = "\n".join(block["text"] for block in final.get("content", [])
                     if block.get("type") == "text" and isinstance(block.get("text"), str))
    return final.get("apiProvider"), text
