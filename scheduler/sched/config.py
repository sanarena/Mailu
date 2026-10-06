"""Environment configuration for the mail scheduler daemon."""
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


class Config:
    def __init__(self):
        self.api_secret = _get('API_SECRET', required=True)
        self.db_path = _get('DB_PATH', '/data/scheduler.db')
        self.web_port = _get_int('WEB_PORT', 8080)
        self.poll_seconds = _get_int('POLL_SECONDS', 30)
        self.postfix_host = _get('POSTFIX_HOST', 'mail-mailu-postfix')
        self.postfix_port = _get_int('POSTFIX_PORT', 25)
        self.dovecot_host = _get('DOVECOT_HOST', 'mail-mailu-dovecot')
        self.dovecot_port = _get_int('DOVECOT_PORT', 143)
        self.local_domains = {
            d.strip().lower()
            for d in _get('LOCAL_DOMAINS', '').split(',') if d.strip()}
        self.max_mime_bytes = _get_int('MAX_MIME_BYTES', 10 * 1024 * 1024)
        self.max_recipients = _get_int('MAX_RECIPIENTS', 200)
        self.max_future_days = _get_int('MAX_FUTURE_DAYS', 366)
        self.retention_days = _get_int('RETENTION_DAYS', 90)
        self.snooze_folder = _get('SNOOZE_FOLDER', 'Snoozed')
        self.scheduled_folder = _get('SCHEDULED_FOLDER', 'Scheduled')
