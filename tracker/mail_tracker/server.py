"""Glue: milter processor, HTTP adapter, retention janitor, main entry.

One process, three threads (milter listener, web listener, janitor).
"""
import logging
import secrets
import signal
import threading
import time
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .db import Store
from .geo import load_geo_db
from .mime import process_body, top_header
from .milter import MilterServer
from .sig import link_signature
from .web import TrackerApp

LOG = logging.getLogger('tracker')


def decode_subject(raw):
    try:
        text = str(make_header(decode_header(raw or '')))
    except Exception:
        text = raw or ''
    return text[:500]


class Processor:
    """Milter callback: decide, inject, register. See milter.MilterSession."""

    def __init__(self, config, store):
        self.config = config
        self.store = store

    def _sender_domain(self, sender):
        return sender.rsplit('@', 1)[-1].lower() if '@' in sender else ''

    def process(self, sender, recipients, headers, body, macros):
        sender = (sender or '').strip().lower()
        if not sender:
            return None, None, 'bounce'
        forced = top_header(headers, 'x-tracker-track').strip().lower()
        if forced == 'no':
            return None, None, 'header-opt-out'
        force_track = forced == 'yes'
        auto = top_header(headers, 'auto-submitted').strip().lower()
        if auto and auto != 'no':
            return None, None, 'auto-submitted'
        if self._sender_domain(sender) not in self.config.local_domains:
            return None, None, 'not-local-sender'
        if not force_track and self.store.sender_opted_out(sender):
            return None, None, 'sender-opt-out'
        clean_rcpts = []
        for rcpt in recipients or []:
            rcpt = (rcpt or '').strip().lower()
            if rcpt and '@' in rcpt:
                clean_rcpts.append(rcpt)
        if not clean_rcpts:
            return None, None, 'no-recipients'
        if len(clean_rcpts) > self.config.max_recipients:
            return None, None, 'too-many-recipients'
        if (not force_track and len(clean_rcpts) == 1
                and self.store.recipient_opted_out(sender, clean_rcpts[0])):
            return None, None, 'recipient-opt-out'

        token = secrets.token_urlsafe(18)
        base = self.config.base_url
        pixel_url = f'{base}/o/{token}.gif'

        def rewrite(url, idx):
            sig = link_signature(self.config.hmac_secret, token, idx, url)
            return f'{base}/c/{token}/{idx}?s={sig}'

        try:
            new_body, links, reason = process_body(
                body or b'', headers, pixel_url, rewrite,
                self.config.track_host, self.config.max_links)
        except Exception:
            LOG.exception('mime processing failed for %s', sender)
            return None, None, 'processing-error'
        if new_body is None:
            return None, None, reason
        if self.config.dry_run:
            return body, None, f'dry-run:{reason}'
        message_id = top_header(headers, 'message-id')[:500]
        try:
            self.store.add_message(
                token, message_id, sender, decode_subject(
                    top_header(headers, 'subject')),
                clean_rcpts, links, len(body or b''))
        except Exception:
            LOG.exception('registering tracked mail failed')
            return None, None, 'db-error'
        return new_body, token, 'ok'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Tracker/1.0'

    def _run(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length > 0 else b''
        flat = {k.lower(): v for k, v in self.headers.items()}
        try:
            remote = self.client_address[0] if self.client_address else ''
        except Exception:
            remote = ''
        status, headers, out = self.server.app.handle(
            self.command, self.path, flat, body, remote)
        try:
            self.send_response(status)
            self.send_header('Content-Length', str(len(out)))
            for key, val in headers.items():
                if key.lower() != 'content-length':
                    self.send_header(key, val)
            self.end_headers()
            self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _run
    do_POST = _run

    def log_message(self, fmt, *args):
        LOG.info('%s %s', self.address_string(), fmt % args)


def run_janitor(store, retention_days, stop):
    while not stop.wait(3600):
        try:
            store.purge_old_events(retention_days)
            LOG.info('retention purge done (days=%d)', retention_days)
        except Exception:
            LOG.exception('retention purge failed')


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    config = Config()
    store = Store(config.db_path)
    geo = load_geo_db(config.geo_db_path)
    LOG.info('geo db: %s', type(geo).__name__)
    processor = Processor(config, store)
    app = TrackerApp(config, store, geo)

    milter_server = MilterServer(('0.0.0.0', config.milter_port), processor,
                                 config.max_mime_bytes)
    web_server = ThreadingHTTPServer(('0.0.0.0', config.web_port), Handler)
    web_server.app = app
    web_server.daemon_threads = True
    stop = threading.Event()

    threads = [
        threading.Thread(target=milter_server.serve_forever,
                         kwargs={'poll_interval': 0.5}, daemon=True),
        threading.Thread(target=web_server.serve_forever,
                         kwargs={'poll_interval': 0.5}, daemon=True),
        threading.Thread(target=run_janitor,
                         args=(store, config.retention_days, stop),
                         daemon=True),
    ]
    for thread in threads:
        thread.start()
    LOG.info('tracker up: milter=%d web=%d dry_run=%s',
             config.milter_port, config.web_port, config.dry_run)

    def shutdown(*_args):
        stop.set()
        milter_server.shutdown()
        web_server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()
    store.close()


if __name__ == '__main__':
    main()
