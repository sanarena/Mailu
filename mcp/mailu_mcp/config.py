"""Shared configuration for the Mailu MCP server (stdlib only)."""
import os
from dataclasses import dataclass, field


@dataclass
class MailConfig:
    imap_host: str = "localhost"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_ssl: bool = True
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    default_from: str = ""


@dataclass
class AdminConfig:
    base_url: str = "https://localhost/api"
    api_token: str = ""
    timeout: int = 30


@dataclass
class GmailConfig:
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    # ... or a full OAuth access token supplied directly
    access_token: str = ""


@dataclass
class AppConfig:
    mail: MailConfig = field(default_factory=MailConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)

    @classmethod
    def from_env(cls) -> "AppConfig":
        mail = MailConfig(
            imap_host=os.getenv("MAILU_IMAP_HOST", "localhost"),
            imap_port=int(os.getenv("MAILU_IMAP_PORT", "993")),
            imap_user=os.getenv("MAILU_IMAP_USER", ""),
            imap_password=os.getenv("MAILU_IMAP_PASSWORD", ""),
            imap_ssl=os.getenv("MAILU_IMAP_SSL", "true").lower() not in ("0", "false", "no"),
            smtp_host=os.getenv("MAILU_SMTP_HOST", "localhost"),
            smtp_port=int(os.getenv("MAILU_SMTP_PORT", "587")),
            smtp_user=os.getenv("MAILU_SMTP_USER", ""),
            smtp_password=os.getenv("MAILU_SMTP_PASSWORD", ""),
            smtp_starttls=os.getenv("MAILU_SMTP_STARTTLS", "true").lower() not in ("0", "false", "no"),
            default_from=os.getenv("MAILU_DEFAULT_FROM", os.getenv("MAILU_IMAP_USER", "")),
        )
        admin = AdminConfig(
            base_url=os.getenv("MAILU_API_BASE", "https://localhost/api").rstrip("/"),
            api_token=os.getenv("MAILU_API_TOKEN", ""),
            timeout=int(os.getenv("MAILU_API_TIMEOUT", "30")),
        )
        gmail = GmailConfig(
            client_id=os.getenv("GMAIL_CLIENT_ID", ""),
            client_secret=os.getenv("GMAIL_CLIENT_SECRET", ""),
            refresh_token=os.getenv("GMAIL_REFRESH_TOKEN", ""),
            access_token=os.getenv("GMAIL_ACCESS_TOKEN", ""),
        )
        return cls(mail=mail, admin=admin, gmail=gmail)


def load_config() -> AppConfig:
    return AppConfig.from_env()
