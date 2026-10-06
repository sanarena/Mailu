"""Mail tracker (Mailbutler-like open/click tracking) for the Mailu setup.

Stdlib-only package. Components:

- milter: sendmail milter server that injects the tracking pixel and
  rewrites links on outbound mail. Fail-open by design.
- web: pixel + click-redirect endpoints and the dashboard JSON API.
- dashboard: static single-page app served by web.
- db, geo, ua, mime, send, server: supporting modules.
"""
