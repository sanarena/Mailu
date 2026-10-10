"""Mailu MCP: mail + admin + Gmail import over stdio or HTTP. Branch: feature/http-auth."""
from . import gmail_import
from . import httpserve
from .admin import AdminClient, AdminError
from .mail import (
    MailConfig,
    MailboxMessage,
    build_search_criteria,
    search_emails,
    get_email,
    list_mailboxes,
    send_email,
    delete_message,
    set_flags,
)
from .config import load_config
from .server import build_server, register_admin_tools, register_gmail_tools

__all__ = [
    "AdminClient",
    "AdminError",
    "MailConfig",
    "MailboxMessage",
    "build_search_criteria",
    "search_emails",
    "get_email",
    "list_mailboxes",
    "send_email",
    "delete_message",
    "set_flags",
    "load_config",
    "build_server",
    "register_admin_tools",
    "register_gmail_tools",
]
