"""Mail tracker (Mailbutler-like open/click tracking) for the Mailu setup.

Stdlib-only package. Components:

- milter: sendmail milter server that injects the tracking pixel and
  rewrites links on outbound mail. Fail-open by design.
- web: pixel + click-redirect endpoints and the service JSON API
  (Roundcube plugin auth; owner-scoped).
- db, geo, ua, mime, send, server: supporting modules.
"""
