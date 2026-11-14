#!/usr/bin/env bash
# Set the IMAP backup target for one or more mailboxes from the command line
# (the same thing webmail Settings > Backup does, for many mailboxes at once).
#
# The target password is read from your terminal (hidden) and piped straight
# to the mail-backup service inside the cluster, which tests it once against
# the target and then stores it encrypted for every mailbox. It is never
# written to a file, put on a command line or printed.
#
# usage:
#   KUBECONFIG=<cluster kubeconfig> backup/tools/set-backup-target.sh \
#       <imap-host> <imap-username> <mailbox> [<mailbox> ...]
# example:
#   backup/tools/set-backup-target.sh imap.gmail.com you@gmail.com sales@example.com
#
# Folders land under Backup/<mailbox> in the target account (Gmail: labels).
set -euo pipefail

if [ $# -lt 3 ]; then
  sed -n '2,17p' "$0"
  exit 2
fi
host=$1
user=$2
shift 2

read -rsp "Password for ${user} on ${host} (hidden): " pw
echo
if [ -z "$pw" ]; then
  echo "no password entered" >&2
  exit 1
fi

printf '%s' "$pw" | kubectl -n mail exec -i deploy/mail-backup -- python3 -c '
import json, os, sys, urllib.error, urllib.request
host, user, mailboxes = sys.argv[1], sys.argv[2], sys.argv[3:]
password = sys.stdin.read()
secret = os.environ["API_SECRET"]

def call(method, path, body):
    req = urllib.request.Request("http://127.0.0.1:8080" + path, method=method,
                                 data=json.dumps(body).encode(),
                                 headers={"X-Backup-Secret": secret,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

status, res = call("POST", "/v1/test", {"user": mailboxes[0], "host": host, "port": 993,
                                         "username": user, "password": password})
print("connection test:", "OK" if res.get("ok") else "FAILED", "-", res.get("message") or res.get("error"))
if not res.get("ok"):
    sys.exit("nothing saved")
for mailbox in mailboxes:
    status, res = call("PUT", "/v1/target", {"user": mailbox, "host": host, "port": 993,
                                             "username": user, "password": password,
                                             "prefix": "Backup/" + mailbox,
                                             "enabled": True, "include_junk": False})
    if status == 200:
        print("%-28s backup on -> %s, folder Backup/%s (first copy starts now)" % (mailbox, host, mailbox))
    else:
        print("%-28s NOT saved: %s" % (mailbox, res.get("error")))
' "$host" "$user" "$@"
unset pw
