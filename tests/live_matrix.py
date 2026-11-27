#!/usr/bin/env python3
"""Opt-in real Codex chats. Uses the signed-in accounts and Factory Standard credits."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import secrets
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bridge as bridge_module
from bridge import Bridge
from gateway import Gateway, tool_catalog
from factory import require_standard

STOCK = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
NATIVE = "gpt-6.2-sol"
MAX_MODELS = ("factory/claude-opus-5-5", "factory/claude-fable-5.2")


class Codex:
    def __init__(self, binary, home):
        self.stderr = (home / "app-server.log").open("w")
        env = dict(os.environ)
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_FACTORY_PICKER"):
            env.pop(name, None)
        env["CODEX_HOME"] = str(home)
        self.process = subprocess.Popen(
            [str(binary), "app-server"], env=env, text=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr)
        self.events = queue.Queue()
        self.counter = 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            for line in self.process.stdout:
                self.events.put(json.loads(line))
        finally:
            self.events.put(None)

    def send(self, **packet):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **packet}) + "\n")
        self.process.stdin.flush()

    def receive(self):
        packet = self.events.get(timeout=495)
        if packet is None:
            raise RuntimeError("Codex app server exited.")
        return packet

    def request(self, method, params):
        self.counter += 1
        self.send(id=self.counter, method=method, params=params)
        while (packet := self.receive()).get("id") != self.counter:
            if "id" in packet and "method" in packet:
                self.send(id=packet["id"], error={"code": -32601, "message": "Unexpected request"})
        if "error" in packet:
            raise RuntimeError(str(packet["error"]))
        return packet["result"]

    def initialize(self):
        self.request("initialize", {"clientInfo": {"name": "factory-bridge-proof", "version": "1.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.send(method="initialized")

    def models(self):
        result, cursor = {}, None
        while True:
            page = self.request("model/list", {"modelProvider": "openai", "limit": 100, "cursor": cursor})
            result.update({row["model"]: row for row in page["data"]})
            cursor = page.get("nextCursor")
            if cursor is None:
                return result

    def thread(self, directory, sandbox):
        return self.request("thread/start", {
            "model": NATIVE, "modelProvider": "openai", "cwd": str(directory),
            "approvalPolicy": "on-request", "sandbox": sandbox,
            "environments": [{"environmentId": "local", "cwd": str(directory)}],
            "baseInstructions": "You are verifying Codex context and tool calls using synthetic data.",
        })["thread"]["id"]

    def turn(self, thread, model, effort, prompt, approval=None):
        self.request("turn/start", {
            "threadId": thread, "input": [{"type": "text", "text": prompt}],
            "collaborationMode": {"mode": "default", "settings": {
                "model": model, "reasoning_effort": effort}},
        })
        approvals = []
        while True:
            packet = self.receive()
            method = packet.get("method")
            if "id" in packet and method:
                params = packet.get("params", {})
                command = params.get("command", "")
                expected = (method == "item/commandExecution/requestApproval" and approval
                            and is_marker_command(command, approval[0]))
                if expected:
                    approvals.append(command)
                self.send(id=packet["id"], result={"decision": "accept" if expected and approval[1] else "decline"})
            if method == "turn/completed" and packet["params"].get("threadId", thread) == thread:
                result = packet["params"]["turn"]
                if result["status"] != "completed":
                    raise RuntimeError(str(result.get("error") or result["status"]))
                if len(approvals) != int(approval is not None):
                    raise RuntimeError("Codex approval count did not match the test.")
                return "\n".join(row.get("text", "") for row in result["items"]
                                 if row.get("type") == "agentMessage")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.stderr.close()

    def compact(self, thread):
        self.request("thread/compact/start", {"threadId": thread})
        while True:
            packet = self.receive()
            if packet.get("method") == "turn/completed" and packet["params"].get("threadId") == thread:
                result = packet["params"]["turn"]
                if result["status"] != "completed":
                    raise RuntimeError("Native compaction failed: " + str(result.get("error")))
                return


@contextmanager
def serving(server):
    with server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield server
        finally:
            server.shutdown()


def is_marker_command(command, filename):
    try:
        words = shlex.split(command)
        if len(words) == 3 and Path(words[0]).name in {"bash", "zsh", "sh"} and words[1] in {"-c", "-lc"}:
            words = shlex.split(words[2])
    except ValueError:
        return False
    return words in (["touch", filename], ["/usr/bin/touch", filename])


def snapshot(gateway):
    quota = gateway.quota(fresh=True)
    require_standard(quota)
    return {**{row["label"]: row["usedPercent"] for row in quota["windows"]},
            "extraUsage": quota["extraUsageBalance"]}


def options(row):
    return [entry["reasoningEffort"] for entry in row["supportedReasoningEfforts"]]


def choose_effort(model, row):
    if model in MAX_MODELS:
        return "max"
    offered = options(row)
    return next((effort for effort in ("low", "minimal", "none") if effort in offered),
                row["defaultReasoningEffort"])


def same_effort(picker, receipt):
    return picker == receipt or (picker == "none" and receipt == "off")


def receipts(gateway):
    path = gateway.state / "receipts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def emit(**row):
    print(json.dumps(row, ensure_ascii=False), flush=True)


def seed(app, workspace, sandbox):
    thread = app.thread(workspace, sandbox)
    marker = "earlier-" + secrets.token_hex(8)
    answer = app.turn(thread, NATIVE, "low", f"Remember {marker} for later. Reply with the marker.")
    if marker not in answer:
        raise RuntimeError("Native GPT did not repeat its seed marker.")
    return thread, marker


def test_model(app, gateway, workspace, thread, context, model, row):
    sequence = secrets.token_hex(4)
    filename = "tool-" + sequence + ".txt"
    marker = "observed-" + secrets.token_hex(8)
    (workspace / filename).write_text(marker)
    count = len(receipts(gateway))
    effort = choose_effort(model, row)
    prompt = (f"Use a Codex shell tool to read {filename} in the current workspace "
              "using cat. Return both its exact contents and the exact marker from the first "
              "message in this chat. Use a real tool call. Reply with the two values only.")
    answer = app.turn(thread, model, effort, prompt)
    proof = receipts(gateway)[count:]
    if len(proof) != 1:
        raise RuntimeError("Expected exactly one verified Factory receipt.")
    proof = proof[0]
    if proof["model"] != model.removeprefix("factory/") or not same_effort(effort, proof["effort"]):
        raise RuntimeError("The receipt model or effort is different from the picker setting.")
    if context not in answer or marker not in answer or proof["codexToolCalls"] < 1:
        raise RuntimeError("Earlier context or the real Codex tool result was missing from the answer.")
    return {"effort": effort, "factoryCredits": proof["factoryCredits"],
            "toolCalls": proof["codexToolCalls"]}


def approval_roundtrip(app, workspace):
    thread, context = seed(app, workspace, "read-only")
    for model, approved in zip(MAX_MODELS, (True, False)):
        filename = ("approved" if approved else "denied") + ".txt"
        answer = app.turn(thread, model, "max", (
            f"Use the Codex shell tool to run exactly touch {filename} in this workspace. "
            "Set sandbox_permissions to require_escalated with a short justification. "
            "If denied, stop. Report the actual outcome and repeat the marker from the first "
            "message. Do not do anything else."), approval=(filename, approved))
        if context not in answer or (workspace / filename).exists() != approved:
            raise RuntimeError("Approval side effect or context was incorrect.")
        emit(check="approval", model=model, decision="accepted" if approved else "denied", status="pass")
    answer = app.turn(thread, NATIVE, "low", "Repeat the exact marker from the first message.")
    actual = app.request("thread/read", {"threadId": thread, "includeTurns": True})["thread"]
    if context not in answer or actual["id"] != thread or len(actual["turns"]) != 4:
        raise RuntimeError("Switching back to GPT did not keep the same chat and history.")
    emit(check="return-to-gpt", status="pass")


def feature_roundtrip(app, home, workspace, mcp_marker):
    thread, context = seed(app, workspace, "read-only")
    tool_marker = "saved-tool-" + secrets.token_hex(12)
    (workspace / "old-result.txt").write_text(tool_marker)
    answer = app.turn(thread, NATIVE, "low", "Use the shell to cat old-result.txt. "
                      "Say only 'stored' and do not repeat its contents in your reply.")
    if tool_marker in answer or "stored" not in answer.lower():
        raise RuntimeError("The native reply did not isolate the old tool output.")
    path, = (home / "sessions").glob(f"*/*/*/rollout-*{thread}.jsonl")
    records = [json.loads(line) for line in path.read_text().splitlines() if tool_marker in line]
    if not records:
        raise RuntimeError("The actual native Codex tool result is absent from the local journal.")
    for record in records:
        item = record["payload"].get("item", record["payload"])
        emit(check="fixture-marker-record", recordType=record["type"], itemType=item.get("type"),
             fields=sorted(item))
    app.compact(thread)
    seen = []
    route = bridge_module.factory_body

    def observe(body, archive, identifier):
        routed = route(body, archive, identifier)
        if identifier == thread:
            payload = json.dumps(routed)
            seen.append((tool_marker in payload, context in payload,
                         [item.get("type") for item in body.get("input", [])]))
        return routed

    bridge_module.factory_body = observe
    try:
        answer = app.turn(thread, MAX_MODELS[0], "low",
                          "From the earlier conversation, give the exact initial marker and the contents "
                          "the shell returned for old-result.txt. Do not call tools on this turn.")
    finally:
        bridge_module.factory_body = route
    if tool_marker not in answer or context not in answer:
        raise RuntimeError(f"Compacted chat missed markers. Bridge inputs: {seen}. "
                           f"Answer retained tool={tool_marker in answer}, chat={context in answer}.")
    emit(check="compacted-context-and-tool-output", status="pass")
    answer = app.turn(thread, MAX_MODELS[1], "low",
                      "Call the Codex MCP tool bridge_fixture lookup_marker. If the tool is deferred, "
                      "use Codex tool discovery to find it. Repeat the exact tool result.")
    if mcp_marker not in answer:
        raise RuntimeError("The model did not retrieve a real local MCP tool result through Codex.")
    emit(check="codex-mcp-tool", status="pass")


def subagent_roundtrip(app, home, workspace, gateway, parent):
    thread, context = seed(app, workspace, "read-only")
    marker = "sol-child-" + secrets.token_hex(8)
    specs, children, tool_results, child_results = [], [], [], []
    rewrite = bridge_module.factory_body
    native = bridge_module.native_body

    def observe(body, archive, identifier):
        if identifier == thread:
            specs.extend(name for name in tool_catalog(body.get("tools", [])) if "agent" in name)
            for item in body.get("input", []):
                if item.get("type") == "function_call_output":
                    tool_results.append(str(item.get("output", ""))[:240])
                if item.get("type") == "agent_message" and str(item.get("author", "")).endswith("/sol_probe"):
                    if marker in json.dumps(item.get("content", [])):
                        child_results.append("completed")
        return rewrite(body, archive, identifier)

    def observe_native(body):
        if body.get("model") == NATIVE and marker in json.dumps(body.get("input", [])):
            children.append(body.get("model"))
        return native(body)

    bridge_module.factory_body = observe
    bridge_module.native_body = observe_native
    count = len(receipts(gateway))
    try:
        answer = app.turn(thread, parent, "low", (
            "I explicitly authorize you to spawn one native Codex subagent. Use spawn_agent with "
            'model "gpt-6.2-sol", reasoning_effort "low" and fork_turns "none" when supported. '
            'If task_name is required, set it to "sol_probe". The entire child task is to reply '
            f"with the exact string {marker}. Wait for its actual result. If the child errors, "
            "report the error instead of guessing. In your answer repeat "
            "the child's exact reply and the code word you remember from the first message."))
    finally:
        bridge_module.factory_body = rewrite
        bridge_module.native_body = native
    evidence = []
    for path in (home / "sessions").glob("*/*/*/rollout-*.jsonl"):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        meta = rows[0].get("payload", {})
        if meta.get("id") == thread:
            continue
        model = [row["payload"].get("model") for row in rows if row["type"] == "turn_context"]
        child_output = [row["payload"].get("item", row["payload"]) for row in rows
                        if row["type"] == "response_item"]
        child_output = [item for item in child_output if item.get("role") == "assistant"]
        if NATIVE in model and marker in json.dumps(child_output):
            evidence.append(meta.get("id"))
    billed = receipts(gateway)[count:]
    if (not children or not (evidence or child_results) or
            marker not in answer or context not in answer or len(billed) != 1
            or billed[0]["model"] != parent.removeprefix("factory/") or billed[0]["codexToolCalls"] < 1):
        raise RuntimeError(f"Native subagent proof failed. Codex agent tools: {sorted(set(specs))}. "
                           f"Native child requests: {len(children)}. Sol child journals: {len(evidence)}. "
                           f"Completed child responses relayed to parent: {len(child_results)}. "
                           f"Observed tool results: {list(dict.fromkeys(tool_results))}. "
                           f"Factory receipts: {[row['codexToolCalls'] for row in billed]}.")
    emit(check="native-sol-subagent", parent=parent, child=NATIVE, relayedTools=billed[0]["codexToolCalls"],
         status="pass")


def run(args, home, workspace, gateway):
    app = Codex(args.binary, home)
    failures = []
    try:
        app.initialize()
        catalog = app.models()
        offered = {name: model for name, model in catalog.items() if name.startswith("factory/")}
        gateway_models = {"factory/" + row["slug"] for row in gateway.catalog()["models"]}
        if set(offered) != gateway_models or not offered:
            raise RuntimeError("Factory gateway and signed Codex picker catalogs did not match.")
        explicit = ["factory/" + model.removeprefix("factory/") for model in args.model]
        selected = sorted(offered) if args.all else explicit or ([] if args.subagents else list(MAX_MODELS))
        if args.shard:
            number, total = args.shard
            selected = selected[number - 1::total]
        if set(selected) - offered.keys():
            raise RuntimeError("A requested model is unavailable in the signed picker.")
        emit(check="catalog", offered=len(offered), selected=len(selected))
        emit(check="quota-before", **snapshot(gateway))
        if args.approvals:
            approval_roundtrip(app, workspace)
        if args.features:
            feature_roundtrip(app, home, workspace, args.mcp_marker)
        if args.subagents:
            subagent_roundtrip(app, home, workspace, gateway, "factory/" + args.subagents)
        for index, model in enumerate(selected):
            if index % 6 == 0:
                thread, marker = seed(app, workspace, "read-only")
            try:
                result = test_model(app, gateway, workspace, thread, marker, model, offered[model])
                emit(model=model, status="pass", **result)
            except (RuntimeError, queue.Empty) as exc:
                failures.append(model)
                emit(model=model, status="fail", reason=str(exc)[:400])
                try:
                    snapshot(gateway)
                except RuntimeError:
                    break
                thread, marker = seed(app, workspace, "read-only")
        emit(check="quota-after", **snapshot(gateway))
        emit(check="summary", selected=len(selected), failed=failures)
    finally:
        app.close()
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run every exact model visible to this account")
    parser.add_argument("--model", action="append", default=[], help="Run one picker ID, repeatable")
    parser.add_argument("--shard", nargs=2, type=int, metavar=("NUMBER", "TOTAL"),
                        help="Run a 1-based shard of the selected model list")
    parser.add_argument("--approvals", action="store_true", help="Also test accept/deny and return to native GPT")
    parser.add_argument("--features", action="store_true", help="Also test compacted tool history and a local MCP tool")
    parser.add_argument("--subagents", metavar="FACTORY_MODEL",
                        help="Test the given Factory parent spawning a native GPT-6.2 Sol subagent")
    parser.add_argument("--subagent-backend", choices=("v1", "v2"), default="v2",
                        help="Select the Codex agent tools used by the subagent test")
    parser.add_argument("--binary", type=Path, default=STOCK)
    args = parser.parse_args()
    if args.shard and not 1 <= args.shard[0] <= args.shard[1]:
        parser.error("--shard NUMBER TOTAL requires 1 <= NUMBER <= TOTAL.")
    auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    if not auth.is_file():
        parser.error("An existing Codex ChatGPT file sign-in is required for this isolated live test.")
    with TemporaryDirectory(prefix="codex-factory-matrix-") as temp:
        directory = Path(temp)
        home, workspace = directory / "codex", directory / "workspace"
        home.mkdir()
        workspace.mkdir()
        (home / "auth.json").symlink_to(auth)
        with serving(Gateway(0, directory / "gateway")) as gateway:
            gateway.qualification = False
            with serving(Bridge(0, gateway.server_port, gateway.state, home)) as bridge:
                args.mcp_marker = "lookup-" + secrets.token_hex(8)
                mcp = ("\n[mcp_servers.bridge_fixture]\n"
                       f"command = {json.dumps(sys.executable)}\n"
                       f"args = {json.dumps([str(ROOT / 'tests/fixture_mcp.py'), args.mcp_marker])}\n"
                       "required = true\n") if args.features else ""
                (home / "config.toml").write_text(
                    f'model = "{NATIVE}"\nmodel_provider = "openai"\n'
                    f'openai_base_url = "{bridge.origin}/v1"\n'
                    'cli_auth_credentials_store = "file"\nmcp_oauth_credentials_store = "file"\n'
                    '[features]\napps = false\nplugins = false\n' +
                    ("multi_agent = true\nmulti_agent_v2 = " +
                     ("true" if args.subagent_backend == "v2" else "false") + "\n"
                     if args.subagents else "") + mcp)
                return run(args, home, workspace, gateway)


if __name__ == "__main__":
    sys.exit(main())
