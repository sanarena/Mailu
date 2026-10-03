"""Mailu MCP server — Gmail-import lane (feature/gmail-import).

Stdio entry point exposing mail tools, the full Mailu admin REST API and
Gmail search/copy-job tools. feature/combined serves this same full set
with unified docs and end-to-end smoke tests.
"""
from typing import Any, Dict

from . import gmail_import as gmail_mod
from .admin import AdminClient, AdminError
from .config import load_config
from . import mail as mail_mod
from .protocol import McpServer

SERVER_NAME = "mailu-gmail-import"
SERVER_VERSION = "0.3.0"


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

    register_admin_tools(server)
    register_gmail_tools(server)
    return server


def register_gmail_tools(server: McpServer) -> None:
    """Register Gmail search + copy-job tools."""

    @server.tool("gmail_search", "Run a Gmail search query, return matching message metadata.",
                 {"type": "object", "required": ["query"],
                  "properties": {
                      "query": {"type": "string",
                                "description": "Gmail search query, e.g. 'from:boss newer_than:30d has:attachment'"},
                      "max_results": {"type": "integer", "default": 50},
                      "label_ids": {"type": "array", "items": {"type": "string"}}}})
    def _gsearch(args):
        cfg = load_config()
        return gmail_mod.gmail_search(cfg.gmail, args["query"],
                                      args.get("max_results", 50),
                                      args.get("label_ids"))

    @server.tool("gmail_import_run", "Copy job: search Gmail by query, APPEND every match verbatim to a Mailu mailbox.",
                 {"type": "object", "required": ["query"],
                  "properties": {
                      "query": {"type": "string"},
                      "dest_mailbox": {"type": "string", "default": "INBOX"},
                      "max_messages": {"type": "integer", "default": 100},
                      "dry_run": {"type": "boolean", "default": False},
                      "job_name": {"type": "string", "default": "default"},
                      "label_ids": {"type": "array", "items": {"type": "string"}},
                      "mark_source_read": {"type": "boolean", "default": False},
                      "skip_existing": {"type": "boolean", "default": True}}})
    def _grun(args):
        app = load_config()
        job = gmail_mod.CopyJob(query=args["query"],
                                dest_mailbox=args.get("dest_mailbox", "INBOX"),
                                max_messages=args.get("max_messages", 100),
                                dry_run=args.get("dry_run", False),
                                job_name=args.get("job_name", "default"),
                                label_ids=args.get("label_ids"),
                                mark_source_read=args.get("mark_source_read", False),
                                skip_existing=args.get("skip_existing", True))
        return gmail_mod.run_copy_job(app, job).as_dict()

    @server.tool("gmail_import_status", "Show copy-job state: how many Gmail IDs already copied.",
                 {"type": "object",
                  "properties": {"job_name": {"type": "string", "default": "default"}}})
    def _gstatus(args):
        return gmail_mod.job_status(args.get("job_name", "default"))


def register_admin_tools(server: McpServer) -> None:
    """Register every Mailu admin REST endpoint as an MCP tool (client built lazily)."""
    bound: Dict[str, Any] = {}

    def _client() -> AdminClient:
        if "client" not in bound:
            bound["client"] = AdminClient(load_config().admin)
        return bound["client"]

    def _call(method: str, args, required=()):
        missing = [k for k in required if k not in args]
        if missing:
            raise ValueError(f"Missing required arguments: {', '.join(missing)}")
        try:
            return getattr(_client(), method)(**args)
        except AdminError as exc:
            raise RuntimeError(f"Admin API error: {exc}") from exc

    specs = [
        # domains
        ("admin_domains_list", "List all domains.", "list_domains", []),
        ("admin_domain_get", "Look up one domain (incl. DNS records).", "get_domain", ["domain"]),
        ("admin_domain_create", "Create a domain: name + comment/max_users/max_aliases/max_quota_bytes/signup_enabled/alternatives.",
         "create_domain", ["name"]),
        ("admin_domain_update", "Update a domain (PATCH).", "update_domain", ["domain"]),
        ("admin_domain_delete", "Delete a domain.", "delete_domain", ["domain"]),
        ("admin_domain_dkim", "Generate new DKIM/DMARC keys for a domain.", "generate_dkim", ["domain"]),
        ("admin_managers_list", "List managers of a domain.", "list_managers", ["domain"]),
        ("admin_manager_add", "Add a domain manager.", "add_manager", ["domain", "user_email"]),
        ("admin_manager_check", "Check whether a user manages a domain.", "check_manager", ["domain", "email"]),
        ("admin_manager_remove", "Remove a domain manager.", "remove_manager", ["domain", "email"]),
        ("admin_domain_users", "List all users of a domain.", "list_domain_users", ["domain"]),
        # alternative domains
        ("admin_alternatives_list", "List all alternative domains.", "list_alternatives", []),
        ("admin_alternative_get", "Look up one alternative domain.", "get_alternative", ["alt"]),
        ("admin_alternative_create", "Create an alternative domain for a domain.", "create_alternative", ["name", "domain"]),
        ("admin_alternative_delete", "Delete an alternative domain.", "delete_alternative", ["alt"]),
        # users (all UserGet/UserCreate/UserUpdate parameters pass through)
        ("admin_users_list", "List all users.", "list_users", []),
        ("admin_user_get", "Look up one user (quota, forwarding, autoreply, spam…).", "get_user", ["email"]),
        ("admin_user_create", "Create a user: email + raw_password + every optional user parameter.",
         "create_user", ["email", "raw_password"]),
        ("admin_user_update", "Update a user (PATCH, any user parameter).", "update_user", ["email"]),
        ("admin_user_delete", "Delete a user.", "delete_user", ["email"]),
        # aliases
        ("admin_aliases_list", "List all aliases.", "list_aliases", []),
        ("admin_alias_get", "Look up one alias.", "get_alias", ["alias"]),
        ("admin_alias_create", "Create an alias: email + destination[] + comment/wildcard/hostname.",
         "create_alias", ["email", "destination"]),
        ("admin_alias_update", "Update an alias (PATCH).", "update_alias", ["alias"]),
        ("admin_alias_delete", "Delete an alias.", "delete_alias", ["alias"]),
        ("admin_aliases_by_domain", "Look up aliases of a domain.", "aliases_by_domain", ["domain"]),
        ("admin_myaliases_list", "List anonymous aliases of the current API user.", "list_my_aliases", []),
        ("admin_myalias_delete", "Permanently delete one of your anonymous aliases.", "delete_my_alias", ["alias"]),
        ("admin_myalias_update", "Modify your anonymous alias (disabled/note/hostname/destination).", "update_my_alias", ["alias"]),
        ("admin_alias_domains_available", "List domains available for alias creation.", "available_alias_domains", []),
        ("admin_random_alias_create", "Create a random anonymous alias (SimpleLogin/Bitwarden compatible; per-user email+token auth, optional note/hostname).",
         "create_random_alias", ["email", "token"]),
        # relays
        ("admin_relays_list", "List all relayed domains.", "list_relays", []),
        ("admin_relay_get", "Look up one relayed domain.", "get_relay", ["name"]),
        ("admin_relay_create", "Create a relayed domain: name + smtp + comment.", "create_relay", ["name"]),
        ("admin_relay_update", "Update a relayed domain (PATCH).", "update_relay", ["name"]),
        ("admin_relay_delete", "Delete a relayed domain.", "delete_relay", ["name"]),
        # tokens
        ("admin_tokens_list", "List all auth tokens.", "list_tokens", []),
        ("admin_token_create", "Create a token for a user (email + comment/AuthorizedIP).", "create_token", ["email"]),
        ("admin_tokens_of_user", "List all tokens of a user.", "tokens_of_user", ["email"]),
        ("admin_user_token_create", "Create a token for a given user path.", "create_user_token", ["email"]),
        ("admin_token_get", "Find a token by id.", "get_token", ["token_id"]),
        ("admin_token_update", "Update a token (comment/AuthorizedIP).", "update_token", ["token_id"]),
        ("admin_token_delete", "Delete a token.", "delete_token", ["token_id"]),
    ]
    for name, desc, method, required in specs:
        schema: dict = {"type": "object"}
        if required:
            schema["required"] = required
            schema["properties"] = {k: {} for k in required}
        else:
            schema["properties"] = {}
        server.tool(name, desc, schema)(
            lambda args, _m=method, _req=required: _call(_m, args, _req))


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
