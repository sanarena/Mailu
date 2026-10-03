"""Mailu MCP: mail tools (IMAP search/read + SMTP send). Branch: feature/mail-mcp."""
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

__all__ = [
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
]
