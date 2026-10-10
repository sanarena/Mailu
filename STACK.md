# Mailu+ stack: mail server with tracking, scheduling, and MCP

This fork runs stock Mailu (`upstream` = `Mailu/Mailu`, base `2024.06`)
plus five custom components, all live in the `mail` namespace of the
Hetzner k3s cluster and deployed via Flux. Everything custom is
stdlib-only Python except the two Roundcube plugins (PHP/JS).

## Components

| Component | Dir | Live version | Image | Tests |
|---|---|---|---|---|
| MCP server (mail + admin + Gmail import, stdio/HTTP) | `mcp/` | server 0.4.0, 53 tools | `sanarena/mailu-mcp:20261004144051` | 77 ([README](mcp/README.md)) |
| Mail tracker (milter + pixel/click + service API) | `tracker/` | 1.0.12 (service-only) | `sanarena/mailu-tracker:1.0.12` | 96 ([README](tracker/README.md)) |
| Scheduler (send-later + snooze daemon) | `scheduler/` | 1.0.4 | `sanarena/mail-scheduler:1.0.4` | 25 ([README](scheduler/README.md)) |
| Roundcube `mail_tracking` plugin | `roundcube-mail-tracking/` | plugin 1.0.7 | `sanarena/roundcube-tracking` (digest-pinned in Flux) | live E2E ([README](roundcube-mail-tracking/README.md)) |
| Roundcube `mail_scheduler` plugin | `roundcube-mail-scheduler/` | plugin 1.0.6 | `sanarena/roundcube-scheduler` (digest-pinned in Flux) | live E2E ([README](roundcube-mail-scheduler/README.md)) |

Total: 198 unit tests, all green (`python3 -m unittest discover -s
<dir>/tests` per component, no live server needed). The Roundcube
plugins have no unit harness (`php -l` + `node --check` only) and are
covered by live browser E2E instead.

Mail flow: postfix → tracker milter (`:8899`, pixel/link injection) →
rspamd (scan + DKIM sign) → out; webmail plugins talk to the tracker
(`:8080`) and scheduler (`:8080`) ClusterIP APIs with shared-secret
auth; the MCP server talks to the public front over TLS
(`imap.arminnikdel.com:993/465`) and to the admin API in-cluster.

## Git layout

- `master` is canonical and contains everything below. There is no
  `main` branch in this repo.
- `feature/*` branches are historic build lanes, all superseded by
  `master`:
  `feature/mail-mcp`, `feature/admin-mcp`, `feature/gmail-import`,
  `feature/combined`, `feature/http-auth` (MCP lanes, oldest → newest)
  and `feature/mail-tracker` (tracker + scheduler + Roundcube work,
  merged last). No branch holds code that `master` lacks — verified by
  diffing every branch tip against the merge (all diffs were older
  subsets: no `timeout`, no import state lock, no per-owner state,
  dashboard-era env).
- `origin` = `sanarena/Mailu` (this fork), `upstream` = `Mailu/Mailu`.

## Cluster topology (`mail` namespace)

Deployments: `mail-mailu-*` (stock Mailu 2024.06.58: admin, dovecot,
front, mariadb, oletools, postfix, redis, rspamd, webmail),
`mail-tracker`, `mail-scheduler`, `mail-mcp`, `mail-tls-bridge`.
Webmail gets the Roundcube plugins via initContainers
(`install-hellomail`, `install-scheduler-plugin`, `install-tracking`)
that copy from the digest-pinned package images; `hellomail` is an
external package, not built from this repo.

Ingresses: tracker pixel/click host (serves already-sent mail;
dashboard retired), `mcp.arminnikdel.com` (MCP over HTTPS, TLS at
ingress). PVCs: `mail-tracker-data`, `mail-scheduler-data`,
`mail-mcp-state` (all single-replica + `Recreate`: RWO SQLite/state,
never two writers).

## Deploy pipeline (source of truth: Flux repo)

Live manifests live in the Flux repo (`mailu/*.yaml`), applied by the
`mailu` Kustomization in namespace `hodlticker`. The `k8s/`
directories in this repo are byte-identical mirrors for reference —
change process runs through Flux, then mirror back here.

1. Build (arm64, Pi build host): `docker build -t
   sanarena/<name>:<ver> <dir> && docker push ...` (tracker needs
   `location.dat` converted first — see [tracker
   README](tracker/README.md); verify `{{.Architecture}}`).
2. Bump the tag in the Flux repo `mailu/` manifests, push
   `origin/main`.
3. `export KUBECONFIG=~/k8s-hetzner/kube/k3s-hetzner/kubeconfig`
   (the default `~/.kube/config` returns `Unauthorized`), then
   `flux reconcile source git flux-system` and
   `flux reconcile kustomization mailu -n hodlticker`.
4. Postfix reads `smtpd_milters` and `always_add_missing_headers` from
   the overrides ConfigMap **at boot only** — after such a change,
   `kubectl -n mail rollout restart deploy/mail-mailu-postfix` (a Flux
   sync alone does not restart it). Same for webmail after a plugin
   package bump.

## Secrets (names only — all live-only, never in git)

| Secret | Keys | Used by |
|---|---|---|
| `tracker-secrets` | `hmac-secret` | tracker (link signatures) |
| `tracker-api` | `api-secret` | tracker + webmail (`tracking-secret.key` file) |
| `scheduler-api` | `api-secret` | scheduler + webmail (`scheduler-secret.key` file) |
| `mailu-creds` | `api-token` | MCP (admin API) |
| `mcp-admin-token` | `token` | MCP (operator Bearer [REDACTED] |

## QA and operations

```bash
python3 -m unittest discover -s mcp/tests        # 77 tests
python3 -m unittest discover -s tracker/tests    # 96 tests
python3 -m unittest discover -s scheduler/tests  # 25 tests
kubectl -n mail exec deploy/mail-mcp -- python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=10).read().decode())"
# same /healthz pattern for mail-scheduler and mail-tracker on :8080
```

- Tracker outage: postfix tempfails + retries while the milter is
  unreachable; to bypass instantly, remove the
  `tracker-postfix-overrides` mount from `mailu/helmrelease.yaml` and
  let Flux roll postfix. Rollback: previously tracked mail keeps
  working while the tracker runs.
- Scheduler: failed sends retry with backoff (5m/30m/2h), give up
  after 24h; MID-less mail is refused at the API (`mid:` synthetics
  never match on the wire).
- GeoIP needs true client IPs end to end (`externalTrafficPolicy:
  Local` + LB proxy-protocol + ingress proxy-protocol CIDR), else
  events log SNAT/LB addresses.

Per-component runbooks (retention, DRY_RUN ramp, milter wire notes,
Message-ID contract, Roundcube integration quirks) live in the linked
READMEs above — this file is the map, they are the territory.

## Verified state

2026-10-10: all 198 tests green; repo `k8s/` mirrors byte-identical to
Flux `mailu/`; every pod `Running` with `200` on `/healthz`; postfix
`smtpd_milters` = tracker then rspamd; live Roundcube plugin files
sha256-identical to this tree; Flux `mailu` Kustomization `Ready` at
the same `origin/main` as the cluster. QA mailbox `qa@arminnikdel.com`
exists for browser/API E2E.
