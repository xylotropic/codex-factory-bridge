from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bridge import ATTACHMENT_NOTICE, Bridge, RouteError, factory_body, native_body, visible_archive
from gateway import Gateway, GatewayError, Turn, tool_catalog

THREAD = "01234567-89ab-cdef-0123-456789abcdef"
CALL = {"type": "function_call", "call_id": "call_abc", "name": "exec_command",
        "arguments": '{"cmd":"pwd"}', "encrypted_function_args": ["opaque"]}
OUTPUT = {"type": "function_call_output", "call_id": "call_abc", "output": "earlier tool result"}
CHECKPOINT = {"type": "compaction", "encrypted_content": "matching"}


def message(role, text):
    return {"type": "message", "role": role, "content": [
        {"type": "input_text" if role == "user" else "output_text", "text": text}]}


def record(kind, payload):
    return {"type": kind, "payload": payload}


class History(unittest.TestCase):
    def test_replay_crosses_checkpoint_with_tool_history(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            diary = home / "sessions/2026/11/26"
            diary.mkdir(parents=True)
            records = [record("session_meta", {"id": THREAD}),
                       record("response_item", message("user", "earlier user message")),
                       record("response_item", {"item": CALL}), record("response_item", OUTPUT),
                       record("response_item", {**OUTPUT, "call_id": "orphan"}),
                       record("response_item", {"type": "reasoning", "encrypted_content": "private"}),
                       record("compacted", {"replacement_history": [CHECKPOINT]})]
            path = diary / f"rollout-example-{THREAD}.jsonl"
            path.write_text("".join(json.dumps(line) + "\n" for line in records))
            after = message("user", "latest question")
            transformed = factory_body({"model": "factory/example", "input": [CHECKPOINT, after]}, home, THREAD)
            self.assertEqual(transformed["model"], "example")
            self.assertEqual(transformed["input"][-1], after)
            self.assertEqual(transformed["input"][2], OUTPUT)
            self.assertNotIn("orphan", str(transformed))
            self.assertNotIn("encrypted", str(transformed))
            self.assertNotIn("private", str(transformed))

    def test_earlier_image_placeholder_and_opaque_state(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64,test"}
        old = {"type": "message", "role": "user", "content": [image]}
        current = message("user", "new")
        result = factory_body({"model": "factory/example", "input": [old, current],
                               "previous_response_id": "resp_native"}, Path(), THREAD)
        self.assertIn(ATTACHMENT_NOTICE, str(result))
        self.assertNotIn("base64", str(result))
        self.assertNotIn("previous_response_id", result)

    def test_no_guess_when_checkpoint_missing(self):
        with self.assertRaises(RouteError):
            visible_archive(Path("/nonexistent").resolve(), THREAD, {"matching"})

    def test_return_to_gpt_preserves_payload_but_strips_local_provider_ids(self):
        item = {**message("assistant", "Factory result"), "id": "msg_factory_local"}
        body, changed = native_body({"previous_response_id": "resp_factory_4", "input": [item]})
        self.assertTrue(changed)
        self.assertNotIn("previous_response_id", body)
        self.assertEqual(body["input"], [message("assistant", "Factory result")])


class Relay(unittest.TestCase):
    def test_tool_catalog_keeps_namespace_function_and_freeform(self):
        schema = {"type": "object", "properties": {"query": {"type": "string"}}}
        catalog = tool_catalog([{"type": "namespace", "name": "project", "tools": [
            {"type": "function", "name": "lookup", "description": "lookup", "parameters": schema},
        ]}, {"type": "custom", "name": "apply_patch"},
            {"type": "tool_search", "description": "search", "parameters": schema}])
        self.assertEqual(catalog["project__lookup"]["namespace"], "project")
        self.assertEqual(catalog["apply_patch"]["kind"], "custom")
        self.assertEqual(catalog["search_codex_tools"]["schema"], schema)

    def test_discovered_tool_requires_codex_to_offer_the_alias(self):
        turn = Turn(Mock(), {"model": "example"}, {}, {
            "default_reasoning_level": "low", "_factory_efforts": {"low": "low"}}, THREAD)
        with self.assertRaisesRegex(GatewayError, "unavailable"):
            turn.call("call_discovered_codex_tool", {"alias": "arbitrary", "arguments": {}})

    def test_cross_thread_and_combined_continuations_rejected(self):
        with tempfile.TemporaryDirectory() as name, Gateway(0, Path(name)) as gateway:
            turn = Mock(thread_id=THREAD)
            gateway.calls["call_abc"] = turn
            self.assertEqual(gateway.continuation([OUTPUT], THREAD), (turn, {"call_abc": OUTPUT["output"]}))
            with self.assertRaisesRegex(GatewayError, "different Codex chat"):
                gateway.continuation([OUTPUT], THREAD + "b")
            gateway.calls["call_other"] = Mock(thread_id=THREAD + "b")
            with self.assertRaisesRegex(GatewayError, "different Codex chat"):
                gateway.continuation([OUTPUT, {**OUTPUT, "call_id": "call_other"}], THREAD)

    def test_duplicate_turn_id_fails_before_second_start(self):
        with tempfile.TemporaryDirectory() as name, Gateway(0, Path(name)) as gateway:
            window = {"usedPercent": 10}
            info = {"default_reasoning_level": "low", "_factory_efforts": {"low": "low"}}
            quota = {"overagePreference": "droidCore", "extraUsageBalance": "0.00", "windows": [window] * 3}
            with patch.object(gateway, "eligible_models", return_value={"example": info}), \
                    patch.object(gateway, "quota", return_value=quota), patch("gateway.threading.Thread"):
                first = gateway.start_turn({"model": "example"}, thread_id=THREAD, turn_id="turn-a")
                gateway.finish_turn(first)
                with self.assertRaisesRegex(GatewayError, "already started"):
                    gateway.start_turn({"model": "example"}, thread_id=THREAD, turn_id="turn-a")
                second = gateway.start_turn({"model": "example"}, thread_id=THREAD + "b", turn_id="turn-a")
                self.assertIs(gateway.relay_turn(second.secret), second)


@contextmanager
def listening(server):
    with server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield server.server_port
        finally:
            server.shutdown()


class LocalEndpoints(unittest.TestCase):
    def test_browser_origin_and_unauthorized_catalog_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with listening(Bridge(0, 1, path, path)) as port:
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/health", headers={"Origin": "https://example.test"})
                self.assertEqual(connection.getresponse().status, 403)
                connection.close()
                connection.request("GET", "/v1/models")
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()


if __name__ == "__main__":
    unittest.main()
