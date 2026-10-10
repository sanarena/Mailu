"""Best-effort mail-client / device parsing from pixel/click User-Agents.

Image-open UAs are often proxies (Gmail, Apple Mail Privacy Protection),
so results are explicitly approximate - same caveat as every tracker.
"""
import re

_RULES = [
    # (pattern, client, device-or-None-to-keep-sniffing)
    (re.compile(r'googleimageproxy|via ggpht\.com', re.I),
     'Gmail image proxy', 'Unknown (proxied)'),
    (re.compile(r'^mozilla/5\.0$', re.I),
     'Generic prefetch proxy', 'Unknown (proxied)'),
    (re.compile(r'outlook-ios', re.I), 'Outlook iOS', 'iPhone/iPad'),
    (re.compile(r'outlook-android', re.I), 'Outlook Android', 'Android'),
    (re.compile(r'microsoft outlook|ms-office|outlook/', re.I),
     'Outlook', None),
    (re.compile(r'thunderbird', re.I), 'Thunderbird', None),
    (re.compile(r'apple mail', re.I), 'Apple Mail', None),
    (re.compile(r'iphone|ipad|ios', re.I), 'iOS Mail', None),
    (re.compile(r'superhuman', re.I), 'Superhuman', None),
    (re.compile(r'hey\.com|spark|afters?hock|canary|edison|newton', re.I),
     'Mobile mail app', None),
]

_DEVICE_RULES = [
    (re.compile(r'iphone', re.I), 'iPhone'),
    (re.compile(r'ipad', re.I), 'iPad'),
    (re.compile(r'macintosh|mac os x', re.I), 'Mac'),
    (re.compile(r'android', re.I), 'Android'),
    (re.compile(r'windows', re.I), 'Windows PC'),
    (re.compile(r'linux|x11|ubuntu', re.I), 'Linux PC'),
    (re.compile(r'mobile', re.I), 'Mobile'),
]


def parse_user_agent(ua):
    """Return (client, device); both 'Unknown' when nothing matches."""
    ua = ua or ''
    client, device = 'Unknown', 'Unknown'
    for pattern, name, fixed_device in _RULES:
        if pattern.search(ua):
            client = name
            if fixed_device:
                return client, fixed_device
            break
    for pattern, name in _DEVICE_RULES:
        if pattern.search(ua):
            device = name
            break
    if client == 'Unknown' and device != 'Unknown':
        client = 'Webmail / browser'
    return client, device
