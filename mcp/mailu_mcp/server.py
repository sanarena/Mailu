"""Mailu MCP server — mail lane (feature/mail-mcp).

Stdio entry point exposing IMAP search/read + SMTP send as MCP tools.
Admin and Gmail-import tools live on their own branches; feature/combined
serves every tool at once.
"""
from .config import load_config
from . import mail as mail_mod
from .protocol import McpServer

SERVER_NAME = "mailu-mail"
SERVER_VERSION = "0.1.0"


def build_server() -> McpServer:
    server = McpServer(SERVER_NAME, SERVER_VERSION)
    cfg = load_config()
    m = cfg.mail

    @server.tool("mail_search", "Search a Mailu mailbox (IMAP SEARCH with friendly filters).",
                 {"type": "object",
                  "properties": {
                      "mailbox": {"type": "string", "default": "INBOX"},
                      "limit": {"type": "integer", "default": 50},
                      "unseen_only": {"type": "boolean"},
                      "seen": {"type": "boolean"},
                      "flagged": {"type": "boolean"},
                      "since": {"type": "string", "description": "e.g. 01-Jan-2025"},
                      "before": {"type": "string"},
                      "from_": {"type": "string"}, "to": {"type": "string"},
                      "cc": {"type": "string"}, "subject": {"type": "string"},
                      "body": {"type": "string"}, "text": {"type": "string"},
                      "header": {"type": "string"}, "header_value": {"type": "string"},
                      "uids": {"type": "array", "items": {"type": "integer"}},
                      "larger": {"type": "integer"}, "smaller": {"type": "integer"}}})
    def _search(args):
        return mail_mod.search_emails(m, args.get("mailbox", "INBOX"),
                                      args.get("limit", 50),
                                      **{k: v for k, v in args.items()
                                         if k not in ("mailbox", "limit")})

    @server.tool("mail_read", "Read one full message: headers, text/html bodies, attachments.",
                 {"type": "object", "required": ["mailbox", "uid"],
                  "properties": {
                      "mailbox": {"type": "string"}, "uid": {"type": "integer"},
                      "mark_seen": {"type": "boolean", "default": False},
                      "include_body": {"type": "boolean", "default": True},
                      "max_bytes": {"type": "integer", "default": 200000}}})
    def _read(args):
        return mail_mod.get_email(m, args["mailbox"], args["uid"],
                                  args.get("mark_seen", False),
                                  args.get("include_body", True),
                                  args.get("max_bytes", 200000))

    @server.tool("mail_send", "Send an email via Mailu SMTP with all details/parameters.",
                 {"type": "object", "required": ["to", "subject"],
                  "properties": {
                      "to": {"type": "array", "items": {"type": "string"}},
                      "subject": {"type": "string"},
                      "body_text": {"type": "string"}, "body_html": {"type": "string"},
                      "from_addr": {"type": "string"},
                      "cc": {"type": "array", "items": {"type": "string"}},
                      "bcc": {"type": "array", "items": {"type": "string"}},
                      "reply_to": {"type": "string"},
                      "in_reply_to": {"type": "string"}, "references": {"type": "string"},
                      "extra_headers": {"type": "object"},
                      "attachments": {"type": "array", "items": {"type": "object"}},
                      "priority": {"type": "string", "enum": ["high", "normal", "low"]}}})
    def _send(args):
        return mail_mod.send_email(m, **args)

    @server.tool("mail_mailboxes", "List IMAP mailboxes.",
                 {"type": "object", "properties": {}})
    def _boxes(args):
        return mail_mod.list_mailboxes(m)

    @server.tool("mail_flag", "Add/remove/set IMAP flags on a message.",
                 {"type": "object", "required": ["mailbox", "uid", "flags"],
                  "properties": {
                      "mailbox": {"type": "string"}, "uid": {"type": "integer"},
                      "flags": {"type": "array", "items": {"type": "string"}},
                      "mode": {"type": "string", "enum": ["add", "remove", "set"],
                               "default": "add"}}})
    def _flag(args):
        return {"ok": mail_mod.set_flags(m, args["mailbox"], args["uid"],
                                         args["flags"], args.get("mode", "add"))}

    @server.tool("mail_move", "Move a message to another mailbox.",
                 {"type": "object", "required": ["mailbox", "uid", "dest_mailbox"],
                  "properties": {
                      "mailbox": {"type": "string"}, "uid": {"type": "integer"},
                      "dest_mailbox": {"type": "string"}}})
    def _move(args):
        return {"ok": mail_mod.move_message(m, args["mailbox"], args["uid"],
                                            args["dest_mailbox"])}

    @server.tool("mail_delete", "Delete a message (expunge by default).",
                 {"type": "object", "required": ["mailbox", "uid"],
                  "properties": {
                      "mailbox": {"type": "string"}, "uid": {"type": "integer"},
                      "expunge": {"type": "boolean", "default": True}}})
    def _delete(args):
        return {"ok": mail_mod.delete_message(m, args["mailbox"], args["uid"],
                                              args.get("expunge", True))}

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
