"""In-memory IMAP fake with the imaplib surface the daemon uses."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import imaplib  # noqa: E402


def _unq(name):
    if name.startswith('"') and name.endswith('"'):
        return re.sub(r'\\(.)', r'\1', name[1:-1])
    return name


class FakeConfig:
    api_secret = 'test-secret'
    dovecot_host = 'dovecot'
    dovecot_port = 143
    local_domains = {'x.test'}
    max_messages_per_run = 500
    max_bytes_per_run = 300 * 1024 * 1024
    max_message_bytes = 35 * 1024 * 1024
    timeout = 5
    poll_seconds = 300
    allow_private_targets = False
    insecure_tls = False


class FakeServer:
    """A mail account: folders -> {uidvalidity, next_uid, msgs[]}."""

    def __init__(self, delim='.', special=None):
        self.delim = delim
        self.special = special or {}
        self.folders = {}
        self.refuse = []        # queue of (typ, text) answers for APPEND
        self.login_ok = True
        self.logins = []

    def add_folder(self, name, uidvalidity=1):
        self.folders.setdefault(name, {'uidvalidity': uidvalidity,
                                       'next_uid': 1, 'msgs': []})
        return self

    def add(self, folder, raw, flags=('\\Seen',),
            date='07-Oct-2026 21:26:16 +0000'):
        box = self.add_folder(folder).folders[folder]
        uid = box['next_uid']
        box['next_uid'] += 1
        box['msgs'].append({'uid': uid, 'flags': list(flags), 'date': date,
                            'raw': raw})
        return uid

    def remove(self, folder, uid):
        box = self.folders[folder]
        box['msgs'] = [m for m in box['msgs'] if m['uid'] != uid]

    def connect(self, *args, **kwargs):
        return FakeConn(self)


class FakeConn:
    def __init__(self, server):
        self.server = server
        self.selected = None
        self.untagged = {}

    def login(self, user, password):
        if not self.server.login_ok:
            raise imaplib.IMAP4.error('[AUTHENTICATIONFAILED] Invalid credentials')
        self.server.logins.append((user, password))
        return 'OK', [b'Logged in']

    def logout(self):
        return 'BYE', [b'bye']

    def list(self, directory='""', pattern='*'):
        s = self.server
        if pattern == '""':
            return 'OK', [f'(\\Noselect) "{s.delim}" ""'.encode()]
        names = list(s.folders)
        if pattern != '*':
            want = _unq(pattern)
            names = [n for n in names if n == want]
        out = []
        for name in names:
            flags = ['\\HasNoChildren'] + s.special.get(name, [])
            shown = f'"{name}"' if ' ' in name else name
            out.append(f'({" ".join(flags)}) "{s.delim}" {shown}'.encode())
        return 'OK', out

    def select(self, mailbox, readonly=False):
        name = _unq(mailbox)
        box = self.server.folders.get(name)
        if box is None:
            return 'NO', [b'no such mailbox']
        self.selected = name
        self.untagged.setdefault('UIDVALIDITY', []).append(
            str(box['uidvalidity']).encode())
        return 'OK', [str(len(box['msgs'])).encode()]

    def response(self, code):
        return code, self.untagged.pop(code, [None])

    def uid(self, command, *args):
        box = self.server.folders[self.selected]
        if command == 'SEARCH':
            crit = args[-1]
            start = int(re.match(r'UID (\d+):\*', crit).group(1))
            uids = [m['uid'] for m in box['msgs'] if m['uid'] >= start]
            if not uids and box['msgs']:
                uids = [box['msgs'][-1]['uid']]  # "n:*" includes max UID
            return 'OK', [' '.join(map(str, uids)).encode()]
        if command == 'FETCH':
            uid = int(args[0])
            for m in box['msgs']:
                if m['uid'] == uid:
                    meta = (f'1 (UID {uid} FLAGS ({" ".join(m["flags"])})'
                            f' INTERNALDATE "{m["date"]}" BODY[]'
                            f' {{{len(m["raw"])}}}').encode()
                    return 'OK', [(meta, m['raw']), b')']
            return 'OK', [None]
        raise AssertionError(command)

    def create(self, mailbox):
        name = _unq(mailbox)
        if name in self.server.folders:
            return 'NO', [b'[ALREADYEXISTS] Mailbox exists']
        self.server.add_folder(name)
        return 'OK', [b'created']

    def append(self, mailbox, flags, date_time, message):
        if self.server.refuse:
            typ, text = self.server.refuse.pop(0)
            return typ, [text]
        name = _unq(mailbox)
        if name not in self.server.folders:
            return 'NO', [b'[TRYCREATE] no such mailbox']
        flag_list = flags.strip('()').split() if flags else []
        self.server.add(name, message, flags=flag_list,
                        date=date_time.strip('"') if date_time else '')
        return 'OK', [b'appended']
