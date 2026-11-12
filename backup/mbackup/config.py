"""Environment configuration for the mail backup daemon."""
import os


def _get(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f'missing required env {name}')
    return value


def _get_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _get_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


class Config:
    def __init__(self):
        self.api_secret = _get('API_SECRET', required=True)
        # Fernet key (urlsafe base64, 32 bytes) for target passwords at rest.
        self.crypt_key = _get('CRYPT_KEY', required=True)
        self.db_path = _get('DB_PATH', '/data/backup.db')
        self.web_port = _get_int('WEB_PORT', 8080)
        self.poll_seconds = _get_int('POLL_SECONDS', 300)
        # Source: Mailu dovecot, trusted-net login (password ignored).
        self.dovecot_host = _get('DOVECOT_HOST', 'mail-mailu-dovecot')
        self.dovecot_port = _get_int('DOVECOT_PORT', 143)
        self.local_domains = {
            d.strip().lower()
            for d in _get('LOCAL_DOMAINS', '').split(',') if d.strip()}
        # Per-user, per-run budgets: big first backups continue next cycle.
        self.max_messages_per_run = _get_int('MAX_MESSAGES_PER_RUN', 500)
        self.max_bytes_per_run = _get_int('MAX_BYTES_PER_RUN',
                                          300 * 1024 * 1024)
        # Larger messages are skipped (and reported), never retried.
        self.max_message_bytes = _get_int('MAX_MESSAGE_BYTES',
                                          35 * 1024 * 1024)
        self.timeout = _get_int('IMAP_TIMEOUT', 60)
        # TEST ONLY knobs (never set in production): allow targets on
        # private/loopback addresses and skip TLS certificate checks.
        self.allow_private_targets = _get_bool('ALLOW_PRIVATE_TARGETS')
        self.insecure_tls = _get_bool('INSECURE_TLS')
