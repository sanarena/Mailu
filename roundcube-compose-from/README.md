# Roundcube `compose_from` plugin

**What it does:** turns the compose **From** field into a combo box. You
can type any address, or open the list (caret, ↓ key, or typing to
filter) and pick one of your identities.

**Why a plugin:** Roundcube only shows a dropdown of identities, even
though its send code already accepts a typed address as a "custom From".

## Behaviour

- **Picking an identity** works exactly like Roundcube's own dropdown,
  including the signature switch.
- **Typing an address**, e.g. `shop@arminnikdel.com` or
  `Shop <shop@arminnikdel.com>`, sends it as the From. The current
  signature and Reply-To stay as they are.
- **The hint below the field** says whether this mailbox may send from
  the typed address. It asks Mailu the same sender-login question
  Postfix asks (`/internal/postfix/sender/login/<address>`), so the hint
  and the server agree. Postfix still enforces it.
- **Drafts keep a typed From.** Roundcube on its own only restores
  identities and would fall back to the default identity.
- **Replies and forwards** use the address the message was sent to
  (e.g. a catch-all address), but only when:
  - no identity matches;
  - the address is on a domain this server hosts
    (`/internal/postfix/domain/<domain>`);
  - this mailbox may send as it.

## Who may send as what (Mailu, not this plugin)

- **Unused addresses on a domain:** the destination of a domain's `%`
  catch-all alias may send as them.
- **A real mailbox X:** mailboxes X forwards to (with "keep a copy") may
  send as X.
- **Anything:** users with "Allow the user to spoof the sender" may send
  as any address.

## Files

- `plugins/compose_from/`:
  - PHP: the `identity_select` hook and the `plugin.compose_from.check`
    action.
  - JS: the combo box, which keeps Roundcube's hidden `<select
    name="_from">` as the submitted field.
  - CSS (light and dark) and `en_US` labels.
- `overrides/compose_from.inc.php`: enables the plugin and sets
  `compose_from_admin_url`. It needs no secret, because the Mailu
  internal endpoints are only reachable from the pod network.
- `Dockerfile`: the package image (`sanarena/roundcube-compose-from`),
  installed by the `install-compose-from` webmail initContainer.

## Checks

- `php -l` on the PHP files (pod PHP) and `node --check` on the JS.
- Live test through the Pi test harness (see `backup/README.md` for the
  harness).
