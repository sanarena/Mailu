"""Environment-based configuration for the mail tracker."""
import os


def _get(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f'missing required env var {name}')
    return val


def _get_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in (
        '1', 'true', 'yes', 'on')


class Config:
    """All tracker settings come from the environment (see .env.example)."""

    def __init__(self):
        self.track_host = _get('TRACK_HOST', 'track.arminnikdel.com')
        self.milter_port = _get_int('MILTER_PORT', 8899)
        self.web_port = _get_int('WEB_PORT', 8080)
        self.db_path = _get('DB_PATH', '/data/tracker.db')
        self.geo_db_path = _get('GEO_DB_PATH', '/app/geo/location.dat')
        self.imap_host = _get('IMAP_HOST', 'imap.arminnikdel.com')
        self.imap_port = _get_int('IMAP_PORT', 993)
        self.smtp_host = _get('SMTP_HOST', 'imap.arminnikdel.com')
        self.smtp_port = _get_int('SMTP_PORT', 465)
        domains = _get('LOCAL_DOMAINS', 'arminnikdel.com')
        self.local_domains = {d.strip().lower() for d in domains.split(',')
                              if d.strip()}
        admins = _get('TRACKER_ADMINS', '')
        self.admins = {a.strip().lower() for a in admins.split(',')
                       if a.strip()}
        self.session_secret = _get('SESSION_SECRET', required=True)
        self.hmac_secret = _get('HMAC_SECRET', required=True)
        self.dry_run = _get_bool('DRY_RUN', False)
        self.header_only = _get_bool('HEADER_ONLY', False)
        self.max_mime_bytes = _get_int('MAX_MIME_BYTES', 10 * 1024 * 1024)
        self.max_recipients = _get_int('MAX_RECIPIENTS', 200)
        self.max_links = _get_int('MAX_LINKS', 500)
        self.retention_days = _get_int('RETENTION_DAYS', 365)
        self.session_hours = _get_int('SESSION_HOURS', 12)

    @property
    def base_url(self):
        return f'https://{self.track_host}'
