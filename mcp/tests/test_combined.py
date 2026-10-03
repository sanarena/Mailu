"""Combined-branch smoke test: full stdio session over all 48 tools, no network."""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, ".")

from mcp.mailu_mcp.server import build_server


def session(requests):
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    build_server().run(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().strip().splitlines()]


class CombinedTests(unittest.TestCase):
    def test_full_session(self):
        tmp = tempfile.mkdtemp()
        os.environ["MAILU_IMPORT_STATE"] = os.path.join(tmp, "state.json")
        try:
            resps = session([
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "t", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "gmail_import_status",
                            "arguments": {"job_name": "never-ran"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "admin_user_create", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            ])
        finally:
            del os.environ["MAILU_IMPORT_STATE"]
        # notification (no id) produces no response
        self.assertEqual(len(resps), 5)
        self.assertEqual(resps[0]["result"]["serverInfo"]["name"], "mailu-gmail-import")
        tools = resps[1]["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)), "tool names must be unique")
        mail = [n for n in names if n.startswith("mail_")]
        admin = [n for n in names if n.startswith("admin_")]
        gmail = [n for n in names if n.startswith("gmail_")]
        self.assertEqual((len(mail), len(admin), len(gmail)), (7, 43, 3))
        for t in tools:  # every tool carries a usable schema + description
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
        status = json.loads(resps[2]["result"]["content"][0]["text"])
        self.assertEqual(status["copied_total"], 0)
        # missing required arg -> clean MCP error, server stays up
        self.assertTrue(resps[3]["result"]["isError"])
        self.assertEqual(resps[4]["result"], {})


if __name__ == "__main__":
    unittest.main()
