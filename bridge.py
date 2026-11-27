#!/usr/bin/env python3
"""Merge Factory into the signed Codex catalog and route its Responses requests."""

import argparse
from collections import deque
from compression import zstd
from hashlib import sha256
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

PREFIX = "factory/"
UPSTREAM = "chatgpt.com"
UPSTREAM_PREFIX = "/backend-api/codex"
MAX_REQUEST = 12 * 1024 * 1024
ARCHIVE_BUDGET = 256 * 1024
TOOL_RECORD_BUDGET = 16 * 1024
THREAD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
CHECKPOINTS = {"compaction", "context_compaction"}
NATIVE_ENDPOINTS = {"models", "responses", "responses/compact", "alpha/search"}
NON_FORWARDED = {"connection", "content-length", "host", "keep-alive", "proxy-authenticate",
                 "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
LOCAL_ITEM_IDS = ("msg_factory_", "fc_factory_")
PORTABLE_TOOLS = {
    "function_call": ("call_id", "name", "namespace", "arguments"),
    "function_call_output": ("call_id", "output"),
    "custom_tool_call": ("call_id", "name", "namespace", "input"),
    "custom_tool_call_output": ("call_id", "output"),
    "tool_search_call": ("call_id", "execution", "arguments"),
    "tool_search_output": ("call_id", "status", "execution", "tools"),
}
TEXT_PARTS = {"input_text", "output_text", "text", "refusal"}
ATTACHMENT_NOTICE = "[Earlier attachment omitted from Factory text replay.]"
CONTEXT_PREFIXES = ("<environment_context>", "<user_instructions>", "<user_settings>",
                    "<skill>", "<permissions instructions>", "# AGENTS.md instructions",
                    "The following is a summary of", "Context checkpoint:")


class RouteError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def archive_records(home, thread_id, depth=0):
    if depth > 8 or not THREAD_ID.fullmatch(thread_id):
        raise RouteError("Codex could not identify the saved conversation for Factory replay.")
    paths = list((home / "sessions").glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
    if len(paths) != 1:
        raise RouteError("Codex could not locate a unique local conversation for Factory replay.")
    with paths[0].open("rb") as stream:
        records = (json.loads(line) for line in stream if line.endswith(b"\n"))
        first = next(records, {})
        meta = first.get("payload", {})
        if first.get("type") != "session_meta" or meta.get("id") != thread_id:
            raise RouteError("The saved conversation does not match the Factory request.")
        base = meta.get("history_base")
        if base:
            parent = base.get("thread_id", "")
            boundary = base.get("end_ordinal_exclusive")
            if not isinstance(boundary, int) or boundary < 1 or parent == thread_id:
                raise RouteError("Codex could not resolve the original conversation for replay.")
            yield from islice(archive_records(home, parent, depth + 1), boundary)
        else:
            yield first
        yield from records


def checkpoint_keys(item):
    if item.get("type") not in CHECKPOINTS:
        return set()
    return {item[field] for field in ("id", "encrypted_content", "encrypted_context")
            if isinstance(item.get(field), str) and item[field]}


def readable_message(item):
    role = item.get("role")
    if item.get("type") != "message" or role not in {"user", "assistant"}:
        return None
    content = []
    for part in item.get("content", []):
        text = part.get("text")
        if part.get("type") in {"input_image", "input_audio", "input_file"}:
            content.append({"type": "input_text" if role == "user" else "output_text",
                            "text": ATTACHMENT_NOTICE})
            continue
        if not isinstance(text, str) or not text:
            continue
        if part.get("type") != ("input_text" if role == "user" else "output_text"):
            continue
        if role == "user" and text.lstrip().startswith(CONTEXT_PREFIXES):
            continue
        content.append({"type": part["type"], "text": text})
    return {"type": "message", "role": role, "content": content} if content else None


def portable_tool(item):
    kind = item.get("type")
    fields = PORTABLE_TOOLS.get(kind)
    if fields is None or not isinstance(item.get("call_id"), str):
        return None
    clean = {"type": kind, **{key: item[key] for key in fields if key in item}}
    if isinstance(clean.get("output"), list):
        clean["output"] = [
            {"type": part["type"], "text": part["text"]}
            if isinstance(part, dict) and part.get("type") in TEXT_PARTS
            and isinstance(part.get("text"), str)
            else {"type": "input_text", "text": ATTACHMENT_NOTICE}
            for part in clean["output"]
        ]
    return clean


def completed_tool_event(payload):
    if payload.get("type") != "item_completed":
        return None
    item = payload.get("item", {})
    kind = item.get("type")
    if kind == "CommandExecution":
        output = item.get("aggregated_output")
        if not isinstance(output, str) or not output:
            return None
        return {"type": "codex_tool_history", "tool": "command execution",
                "command": item.get("command"), "status": item.get("status"),
                "exit_code": item.get("exit_code"), "output": output}
    if kind not in {"McpToolCall", "DynamicToolCall", "FunctionCallOutput"}:
        return None
    result = item.get("result") or {}
    content = result.get("content", item.get("content_items", item.get("output")))
    if isinstance(content, list):
        content = [
            part["text"] for part in content if isinstance(part, dict)
            and part.get("type") in TEXT_PARTS | {"inputText", "text"}
            and isinstance(part.get("text"), str)
        ]
    if not content or not isinstance(content, (list, str)):
        return None
    return {"type": "codex_tool_history", "tool": item.get("tool") or item.get("name"),
            "namespace": item.get("server") or item.get("namespace"), "status": item.get("status"),
            "arguments": item.get("arguments"), "output": content}


def archive_size(item):
    return len(json.dumps(item, ensure_ascii=False).encode())


def clip_tool_record(item):
    if archive_size(item) <= TOOL_RECORD_BUDGET:
        return item, False
    clipped = dict(item)
    for field in ("output", "input", "arguments", "tools", "command"):
        value = clipped.get(field)
        if value is None or archive_size(value) <= 4096:
            continue
        blob = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode()
        clipped[field] = (blob[:1800].decode(errors="ignore") + "\n[Archived tool detail clipped.]\n"
                          + blob[-1800:].decode(errors="ignore"))
    return clipped, True


def visible_archive(home, thread_id, marker):
    messages = []
    tools = deque()
    message_size = tool_size = omitted_tools = clipped_tools = 0
    calls = set()
    for index, record in enumerate(archive_records(home, thread_id)):
        kind, payload = record.get("type"), record.get("payload", {})
        if kind == "event_msg" and payload.get("type") == "thread_rolled_back":
            raise RouteError("This conversation contains a rollback that cannot yet be replayed in Factory.")
        if kind == "compacted" and any(
            checkpoint_keys(entry.get("item", entry)) & marker
            for entry in payload.get("replacement_history") or []
        ):
            if message_size > ARCHIVE_BUDGET:
                raise RouteError("The saved readable message history exceeds the Factory replay limit.")
            if not any(message["role"] == "user" for _, message in messages):
                raise RouteError("The saved checkpoint contains no readable user history.")
            reconstructed = sorted([*messages, *((ordinal, entry) for ordinal, entry, _ in tools)])
            if omitted_tools or clipped_tools:
                reconstructed.append((index, {"type": "codex_replay_note",
                    "text": (f"Factory replay omitted {omitted_tools} earlier tool records and clipped "
                             f"{clipped_tools} large tool records to fit its context budget.")}))
            return [entry for _, entry in reconstructed]
        if kind == "response_item":
            item = payload.get("item", payload)
            message = readable_message(item) or portable_tool(item)
        elif kind == "event_msg":
            message = completed_tool_event(payload)
        else:
            message = None
        if not message:
            continue
        if message["type"] == "message":
            message_size += archive_size(message)
            if message_size <= ARCHIVE_BUDGET:
                messages.append((index, message))
        else:
            if message["type"].endswith("_call"):
                calls.add(message["call_id"])
            if message["type"].endswith("_output") and message["call_id"] not in calls:
                continue
            message, clipped = clip_tool_record(message)
            size = archive_size(message)
            clipped_tools += clipped
            tools.append((index, message, size))
            tool_size += size
        while tools and message_size + tool_size > ARCHIVE_BUDGET:
            _, _, dropped = tools.popleft()
            tool_size -= dropped
            omitted_tools += 1
    raise RouteError("Codex could not match the saved compaction checkpoint for Factory replay.")


def factory_body(body, home, thread_id):
    body = {**body, "model": body["model"][len(PREFIX):]}
    if not str(body.get("previous_response_id", "")).startswith("resp_factory_"):
        body.pop("previous_response_id", None)
    items = body.get("input")
    if not isinstance(items, list):
        return body
    index = next((i for i in range(len(items) - 1, -1, -1)
                  if items[i].get("type") in CHECKPOINTS), None)
    if index is not None:
        marker = checkpoint_keys(items[index])
        if not marker:
            raise RouteError("The stock Codex checkpoint has no readable archive identifier.")
        original = visible_archive(home, thread_id, marker)
        prefix = [item for item in items[:index]
                  if item.get("role") in {"developer", "system"} or item.get("type") == "additional_tools"]
        items = prefix + original + items[index + 1:]
    last_user = max((i for i, item in enumerate(items) if item.get("role") == "user"
                     and readable_message(item)), default=-1)
    portable = []
    for i, current in enumerate(items):
        if current.get("type") in CHECKPOINTS | {"reasoning"}:
            continue
        item = portable_tool(current) or {
            key: value for key, value in current.items()
            if key not in {"encrypted_content", "internal_chat_message_metadata_passthrough"}
        }
        content = item.get("content")
        if i < last_user and item.get("type") == "message" and isinstance(content, list):
            item["content"] = [
                part if part.get("type") in TEXT_PARTS else
                {"type": "input_text" if item.get("role") == "user" else "output_text",
                 "text": ATTACHMENT_NOTICE}
                for part in content
            ]
        portable.append(item)
    body["input"] = portable
    return body


def native_body(body):
    if not isinstance(body.get("input"), list):
        return body, False
    items = body["input"]
    changed = (any(str(item.get("id", "")).startswith(LOCAL_ITEM_IDS) for item in items)
               or str(body.get("previous_response_id", "")).startswith("resp_factory_"))
    if not changed:
        return body, False
    body = dict(body)
    body["input"] = [{k: v for k, v in item.items()
                      if k != "id" or not str(v).startswith(LOCAL_ITEM_IDS)} for item in items]
    if str(body.get("previous_response_id", "")).startswith("resp_factory_"):
        body.pop("previous_response_id")
    return body, True


class Bridge(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, gateway, state, home):
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.gateway = gateway
        self.state = state
        self.home = home
        self.authenticated = {}
        self.auth_lock = threading.Lock()
        self.ready_catalog = None

    def local_gateway(self):
        token = self.state / "token"
        if token.stat().st_mode & 0o077:
            raise RouteError("Factory's local gateway credential has unsafe permissions.", 503)
        return HTTPConnection("127.0.0.1", self.gateway, timeout=480), {
            "Authorization": "Bearer " + token.read_text().strip(),
        }

    def native(self):
        return HTTPSConnection(UPSTREAM, timeout=480)

    def remember(self, auth):
        with self.auth_lock:
            now = time.monotonic()
            self.authenticated = {key: end for key, end in self.authenticated.items() if end > now}
            self.authenticated[sha256(auth.encode()).digest()] = now + 900

    def known(self, auth):
        with self.auth_lock:
            return self.authenticated.get(sha256(auth.encode()).digest(), 0) > time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def json_reply(self, status, value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.handle_route()

    def do_POST(self):
        self.handle_route()

    def handle_route(self):
        path = urlsplit(self.path)
        try:
            if (self.headers.get("Host") != urlsplit(self.server.origin).netloc
                    or self.headers.get("Origin") is not None
                    or self.headers.get("Sec-Fetch-Site") not in (None, "none")):
                raise RouteError("This bridge accepts local Codex client requests.", 403)
            if self.command == "GET" and path.path == "/health":
                self.json_reply(200, {"status": "ready", "lastCatalog": self.server.ready_catalog})
                return
            if not path.path.startswith("/v1/") or path.path[4:] not in NATIVE_ENDPOINTS:
                raise RouteError("Unknown Codex bridge endpoint.", 404)
            endpoint = path.path[4:]
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or len(auth) <= 7:
                raise RouteError("A signed-in Codex session is required.", 401)
            if self.command == "GET":
                if endpoint == "responses":
                    raise RouteError("Use Codex's HTTP streaming transport for this route.", 426)
                if endpoint != "models":
                    raise RouteError("Unknown Codex bridge method.", 405)
                self.models(auth, path.query)
                return
            if endpoint == "models":
                raise RouteError("Unknown Codex bridge method.", 405)
            raw, body = self.body()
            model = body.get("model")
            if isinstance(model, str) and model.startswith(PREFIX):
                if endpoint != "responses":
                    raise RouteError("Factory native compaction and hosted search are unavailable.")
                if not self.server.known(auth):
                    self.validate_session(auth)
                thread_id = self.headers.get("thread-id") or self.headers.get("session-id", "")
                payload = factory_body(body, self.server.home, thread_id)
                connection, headers = self.server.local_gateway()
                try:
                    if THREAD_ID.fullmatch(thread_id):
                        headers["X-Codex-Thread-ID"] = thread_id
                    metadata = self.headers.get("x-codex-turn-metadata", "{}")
                    try:
                        turn_id = json.loads(metadata).get("turn_id", "")
                    except (ValueError, AttributeError):
                        turn_id = ""
                    if isinstance(turn_id, str) and THREAD_ID.fullmatch(turn_id):
                        headers["X-Codex-Turn-ID"] = turn_id
                    connection.request("POST", "/v1/responses", json.dumps(payload).encode(),
                                       {**headers, "Content-Type": "application/json"})
                    self.relay(connection.getresponse())
                finally:
                    connection.close()
                return
            body, changed = native_body(body)
            headers = self.native_headers()
            if changed:
                headers.pop("content-encoding", None)
                raw = json.dumps(body).encode()
            connection = self.server.native()
            try:
                connection.request("POST", self.target(endpoint, path.query), raw, headers)
                self.relay(connection.getresponse())
            finally:
                connection.close()
        except (BrokenPipeError, ConnectionResetError):
            return
        except RouteError as exc:
            self.json_reply(exc.status, {"error": {"type": "invalid_request_error", "message": str(exc)}})
        except (OSError, HTTPException, ValueError, KeyError):
            self.json_reply(502, {"error": {"type": "server_error", "message": "The Codex bridge could not complete this request."}})

    def native_headers(self):
        return {key.lower(): value for key, value in self.headers.items()
                if key.lower() not in NON_FORWARDED | {"accept-encoding"}}

    def target(self, endpoint, query=""):
        return f"{UPSTREAM_PREFIX}/{endpoint}" + (f"?{query}" if query else "")

    def native_catalog(self, query):
        headers = self.native_headers()
        headers.pop("if-none-match", None)
        connection = self.server.native()
        try:
            connection.request("GET", self.target("models", query), headers=headers)
            response = connection.getresponse()
            return response.status, response.read(MAX_REQUEST)
        finally:
            connection.close()

    def validate_session(self, auth):
        status, _ = self.native_catalog("client_version=0.171.0")
        if status != 200:
            raise RouteError("OpenAI could not verify the signed-in Codex session.", 401)
        self.server.remember(auth)

    def models(self, auth, query):
        status, data = self.native_catalog(query)
        if status != 200:
            self.json_reply(status, json.loads(data))
            return
        self.server.remember(auth)
        catalog = json.loads(data)
        connection = None
        try:
            connection, headers = self.server.local_gateway()
            connection.request("GET", "/v1/models", headers=headers)
            response = connection.getresponse()
            available = json.loads(response.read(MAX_REQUEST)) if response.status == 200 else {}
        except (OSError, HTTPException, ValueError, RouteError):
            available = {}
        finally:
            if connection is not None:
                connection.close()
        rows = [{**row, "slug": PREFIX + row["slug"],
                 "display_name": row["display_name"] + " · Factory"}
                for row in available.get("models", [])]
        self.server.ready_catalog = {"native": len(catalog["models"]),
                                     "factory": len(rows), "updatedAt": int(time.time())}
        self.json_reply(200, {**catalog, "models": catalog["models"] + rows})

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= MAX_REQUEST:
            raise RouteError("Invalid Codex request size.", 413)
        raw = self.rfile.read(size)
        encoding = self.headers.get("Content-Encoding", "identity").lower()
        decoded = raw
        if encoding == "zstd":
            decoder = zstd.ZstdDecompressor()
            decoded = decoder.decompress(raw, max_length=MAX_REQUEST + 1)
            if not decoder.eof or decoder.unused_data:
                raise RouteError("Invalid or oversized compressed Codex request.", 413)
        elif encoding != "identity":
            raise RouteError("Unsupported Codex request encoding.", 415)
        if len(decoded) > MAX_REQUEST:
            raise RouteError("Invalid Codex request size.", 413)
        body = json.loads(decoded)
        if not isinstance(body, dict):
            raise RouteError("Expected a Codex request object.")
        return raw, body

    def relay(self, response):
        self.send_response(response.status)
        for header, value in response.getheaders():
            if header.lower() not in NON_FORWARDED:
                self.send_header(header, value)
        self.send_header("Connection", "close")
        self.end_headers()
        # Preserve streaming and close the upstream socket promptly on cancellation.
        try:
            while chunk := response.read1(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, HTTPException):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17383)
    parser.add_argument("--gateway", type=int, default=17382)
    parser.add_argument("--state", type=Path, default=Path.home() / "Library/Application Support/Factory Codex")
    parser.add_argument("--home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    with Bridge(args.port, args.gateway, args.state, args.home) as bridge:
        bridge.serve_forever()


if __name__ == "__main__":
    main()
