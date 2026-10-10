"""Shared HMAC helpers (click-link signatures)."""
import hashlib
import hmac


def link_signature(secret, token, idx, url):
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(f'{token}:{idx}:{url}'.encode())
    return mac.hexdigest()[:24]
