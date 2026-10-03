"""Mailu MCP server — HTTP lane (feature/http-auth).

Stdio (default) or HTTP (`--http`) entry point exposing mail tools, the
full Mailu admin REST API and Gmail search/copy-job tools. HTTP mode is
built for public load balancers: Bearer-token auth on /mcp, /healthz
for probes, plain HTTP (terminate TLS at the LB).
"""
from typing import Any, Dict

from . import gmail_import as gmail_mod
from .admin import AdminClient, AdminError
from .config import load_config
from . import mail as mail_mod
from .protocol import McpServer

SERVER_NAME = "mailu"
SERVER_VERSION = "0.4.0"


def build_server(ctx=None) -> McpServer:
    """Build the full tool registry.

    ``ctx`` (RequestContext, HTTP mode) carries the caller's mailbox
    identity so each Basic-auth user gets their own IMAP/SMTP session and
    their own Gmail-import namespace; when omitted, the server environment
    identity is used (stdio / operator).
    """
    server = McpServer(SERVER_NAME, SERVER_VERSION)
    m = ctx.mail_config if ctx and ctx.mail_config is not None else load_config().mail

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
    register_gmail_tools(server, m, ctx)
    return server


GMAIL_CRED_SCHEMA = {
    "gmail_access_token": {"type": "string",
                           "description": "Your own Gmail OAuth access token (preferred per-call credential)"},
    "gmail_client_id": {"type": "string"},
    "gmail_client_secret": {"type": "string"},
    "gmail_refresh_token": {"type": "string",
                            "description": "OAuth refresh set; must come with its own client_id + client_secret"},
}


def register_gmail_tools(server: McpServer, mail_config, ctx=None) -> None:
    """Register Gmail search + copy-job tools.

    Gmail credentials resolve per call: any ``gmail_*`` credential argument
    selects a caller-supplied set (all-or-nothing — a refresh token only
    works with the OAuth client that issued it); otherwise the server
    environment set is used, except for HTTP users, who must always bring
    their own so they can never touch the operator's Gmail. Copy jobs land
    in the caller's own mailbox and state is namespaced per owner.
    """
    owner = ctx.email if ctx and ctx.role == "user" else ""
    allow_server = ctx.allow_server_gmail if ctx else True

    def _gmail_cfg(args):
        from .config import GmailConfig
        if any(args.get(k) for k in GMAIL_CRED_SCHEMA):
            return GmailConfig(
                access_token=args.get("gmail_access_token", ""),
                client_id=args.get("gmail_client_id", ""),
                client_secret=args.get("gmail_client_secret", ""),
                refresh_token=args.get("gmail_refresh_token", ""))
        if allow_server:
            return load_config().gmail
        raise gmail_mod.GmailError(
            "No Gmail credentials: pass gmail_access_token (or gmail_client_id + "
            "gmail_client_secret + gmail_refresh_token) with every call")

    @server.tool("gmail_search", "Run a Gmail search query, return matching message metadata.",
                 {"type": "object", "required": ["query"],
                  "properties": {
                      "query": {"type": "string",
                                "description": "Gmail search query, e.g. 'from:boss newer_than:30d has:attachment'"},
                      "max_results": {"type": "integer", "default": 50,
                                        "description": "Capped at 2000 per call"},
                      "label_ids": {"type": "array", "items": {"type": "string"}},
                      **GMAIL_CRED_SCHEMA}})
    def _gsearch(args):
        return gmail_mod.gmail_search(_gmail_cfg(args), args["query"],
                                      args.get("max_results", 50),
                                      args.get("label_ids"))

    @server.tool("gmail_import_run", "Copy job: search Gmail by query, APPEND every match verbatim to your Mailu mailbox.",
                 {"type": "object", "required": ["query"],
                  "properties": {
                      "query": {"type": "string"},
                      "dest_mailbox": {"type": "string", "default": "INBOX"},
                      "max_messages": {"type": "integer", "default": 100,
                                        "description": "Capped at 2000 per job"},
                      "dry_run": {"type": "boolean", "default": False},
                      "job_name": {"type": "string", "default": "default"},
                      "label_ids": {"type": "array", "items": {"type": "string"}},
                      "mark_source_read": {"type": "boolean", "default": False},
                      "skip_existing": {"type": "boolean", "default": True},
                      **GMAIL_CRED_SCHEMA}})
    def _grun(args):
        from .config import AppConfig
        base = load_config()
        app = AppConfig(mail=mail_config, admin=base.admin,
                        gmail=_gmail_cfg(args), http=base.http)
        job = gmail_mod.CopyJob(query=args["query"],
                                dest_mailbox=args.get("dest_mailbox", "INBOX"),
                                max_messages=args.get("max_messages", 100),
                                dry_run=args.get("dry_run", False),
                                job_name=args.get("job_name", "default"),
                                label_ids=args.get("label_ids"),
                                mark_source_read=args.get("mark_source_read", False),
                                skip_existing=args.get("skip_existing", True),
                                owner=owner)
        return gmail_mod.run_copy_job(app, job).as_dict()

    @server.tool("gmail_import_status", "Show your copy-job state: how many Gmail IDs already copied.",
                 {"type": "object",
                  "properties": {"job_name": {"type": "string", "default": "default"}}})
    def _gstatus(args):
        return gmail_mod.job_status(args.get("job_name", "default"),
                                    owner=owner)


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


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Mailu MCP server (stdio or HTTP)")
    ap.add_argument("--http", action="store_true",
                    help="serve JSON-RPC over HTTP instead of stdio")
    ap.add_argument("--host", default=None,
                    help="HTTP bind address (default: MCP_HTTP_HOST or 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None,
                    help="HTTP port (default: MCP_HTTP_PORT or 8000)")
    args = ap.parse_args(argv)
    if args.http:
        from .httpserve import serve_http
        cfg = load_config().http
        # Pass the factory, not an instance: each HTTP request needs a
        # server built for its own RequestContext (caller identity).
        serve_http(build_server, args.host or cfg.host, args.port or cfg.port,
                   cfg.token, cfg.max_body)
    else:
        build_server().run()


if __name__ == "__main__":
    main()
