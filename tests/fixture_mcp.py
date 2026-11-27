"""Local MCP server that returns a disposable marker to Codex."""

import json
import sys


def result(packet):
    match packet["method"]:
        case "initialize":
            return {"protocolVersion": packet["params"]["protocolVersion"],
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "bridge_fixture", "version": "1"}}
        case "tools/list":
            return {"tools": [{"name": "lookup_marker", "description": "Return the disposable test marker.",
                               "inputSchema": {"type": "object", "properties": {}},
                               "annotations": {"readOnlyHint": True}}]}
        case "tools/call":
            if packet["params"]["name"] == "lookup_marker":
                return {"content": [{"type": "text", "text": sys.argv[1]}]}
        case "ping":
            return {}
    raise ValueError("Unsupported fixture method.")


for line in sys.stdin:
    packet = json.loads(line)
    if "id" in packet:
        try:
            response = {"result": result(packet)}
        except ValueError:
            response = {"error": {"code": -32601, "message": "Unsupported fixture method."}}
        print(json.dumps({"jsonrpc": "2.0", "id": packet["id"], **response}), flush=True)
