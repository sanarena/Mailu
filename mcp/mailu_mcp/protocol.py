"""Minimal MCP (Model Context Protocol) stdio transport — stdlib only.

Speaks JSON-RPC 2.0 over newline-delimited stdio with the MCP method set:
initialize, tools/list, tools/call. Incoming notifications get no reply.
"""
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"


@dataclass
class RequestContext:
    """Per-request identity for HTTP mode (see httpserve).

    role="user" carries the Basic-auth caller's mailbox config and forbids
    falling back to the server's Gmail credentials; role="admin" (or None
    context, i.e. stdio) uses the server environment for everything.
    """
    role: str = "admin"
    email: str = ""
    mail_config: Any = None
    allow_server_gmail: bool = True


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]


@dataclass
class McpServer:
    name: str
    version: str = "0.1.0"
    tools: Dict[str, Tool] = field(default_factory=dict)

    def tool(self, name: str, description: str, input_schema: Dict[str, Any]):
        def deco(fn: Callable[[Dict[str, Any]], Any]):
            self.tools[name] = Tool(name, description, input_schema, fn)
            return fn
        return deco

    # -- JSON-RPC plumbing -------------------------------------------------
    def _tool_list(self) -> List[Dict[str, Any]]:
        return [{"name": t.name, "description": t.description,
                 "inputSchema": t.input_schema} for t in self.tools.values()]

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        def ok(result: Any) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        def err(code: int, message: str) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": code, "message": message}}

        if method == "initialize":
            return ok({"protocolVersion": PROTOCOL_VERSION,
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": self.name, "version": self.version}})
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "tools/list":
            return ok({"tools": self._tool_list()})
        if method == "tools/call":
            if not isinstance(params, dict):
                return err(-32602, "Invalid params: object expected")
            name = params.get("name", "")
            args = params.get("arguments") or {}
            tool = self.tools.get(name)
            if tool is None:
                return err(-32602, f"Unknown tool: {name}")
            try:
                result = tool.handler(args)
            except (ValueError, LookupError) as exc:
                return ok({"content": [{"type": "text", "text": f"Error: {exc}"}],
                           "isError": True})
            except Exception as exc:  # never crash the loop on a tool failure
                return ok({"content": [{"type": "text",
                                        "text": f"Tool {name} failed: {exc}"}],
                           "isError": True})
            text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
            return ok({"content": [{"type": "text", "text": text}]})
        if method == "ping":
            return ok({})
        if msg_id is None:  # unknown notification
            return None
        return err(-32601, f"Method not found: {method}")

    def run(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700,
                                                   "message": f"Parse error: {exc}"}}) + "\n")
                stdout.flush()
                continue
            if not isinstance(msg, dict):
                # JSON-RPC batch arrays are not supported; reject, never crash.
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32600,
                                                   "message": "Invalid Request: batch not supported"}}) + "\n")
                stdout.flush()
                continue
            try:
                resp = self.handle(msg)
            except Exception as exc:
                msg_id = msg.get("id") if isinstance(msg, dict) else None
                resp = {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603, "message": str(exc)}}
            if resp is not None:
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()
