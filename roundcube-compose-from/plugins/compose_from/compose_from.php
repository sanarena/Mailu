<?php
// SPDX-License-Identifier: GPL-3.0-or-later

/**
 * compose_from: an editable From field in compose.
 *
 * Roundcube renders From as a plain identity <select>, although its send
 * code already accepts any typed address ("custom from"). This plugin turns
 * the field into a combo box (type any address or pick an identity) and:
 *  - keeps a typed From when a draft is reopened (Roundcube only restores
 *    identities and would fall back to the default one);
 *  - replies from the address a message was sent to (e.g. a catch-all
 *    address) when no identity matches, the address is on a domain this
 *    server hosts and this mailbox may send as it;
 *  - tells the user whether the mailbox may send as a typed address.
 *
 * Permission is Mailu's own: the same sender-login lookup postfix enforces
 * (/internal/postfix/sender/login/<address>), so the hint and reality
 * cannot disagree. Postfix still rejects anything not allowed.
 */
class compose_from extends rcube_plugin
{
    public $task = 'mail';

    /** @var rcmail */
    private $rc;
    private $domain_cache = [];

    public function init()
    {
        $this->rc = rcmail::get_instance();
        $this->add_hook('identity_select', [$this, 'identity_select']);
        $this->register_action('plugin.compose_from.check', [$this, 'action_check']);

        if ($this->rc->action == 'compose' && $this->rc->output->type == 'html') {
            $this->add_texts('localization', true);
            $this->include_script('compose_from.js');
            $this->include_stylesheet($this->local_skin_path() . '/compose_from.css');
        }
    }

    // -- Mailu lookups ------------------------------------------------

    private function owner()
    {
        return strtolower(trim((string) $this->rc->user->get_username()));
    }

    /** Body of a Mailu internal lookup, or null (404 / unreachable). */
    private function admin_get($path)
    {
        $base = rtrim((string) $this->rc->config->get('compose_from_admin_url'), '/');
        if ($base === '') {
            return null;
        }
        $ch = curl_init($base . $path);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, 5);
        $body = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($body === false || $code == 0) {
            return false;  // unreachable
        }
        return $code == 200 ? json_decode((string) $body, true) : null;
    }

    /** true / false, or null when Mailu could not be asked. */
    private function may_send_as($address)
    {
        $res = $this->admin_get('/internal/postfix/sender/login/' . rawurlencode($address));
        if ($res === false) {
            return null;
        }
        if (!is_string($res)) {
            return false;
        }
        $logins = array_map('trim', explode(',', strtolower($res)));
        return in_array($this->owner(), $logins, true);
    }

    private function hosted_domain($address)
    {
        $domain = strtolower(substr(strrchr($address, '@'), 1));
        if ($domain === '') {
            return false;
        }
        if (!array_key_exists($domain, $this->domain_cache)) {
            $res = $this->admin_get('/internal/postfix/domain/' . rawurlencode($domain));
            $this->domain_cache[$domain] = is_string($res) && $res !== '';
        }
        return $this->domain_cache[$domain];
    }

    // -- compose ------------------------------------------------------

    /**
     * Runs when Roundcube picks the From identity. If none matched, look
     * for the From this message really calls for and hand it to the JS.
     */
    public function identity_select($args)
    {
        if ($args['selected'] !== null || empty($args['message']) || empty($args['message']->headers)) {
            return $args;
        }
        $compose_id = rcube_utils::get_input_string('_id', rcube_utils::INPUT_GET);
        $compose = $compose_id ? ($_SESSION['compose_data_' . $compose_id] ?? []) : [];
        $mode = $compose['mode'] ?? '';
        $headers = $args['message']->headers;
        $charset = $headers->charset;
        $own = [];
        foreach ((array) $args['identities'] as $ident) {
            $own[strtolower($ident['email_ascii'] ?? $ident['email'])] = true;
        }

        $custom = null;
        if ($mode == rcmail_sendmail::MODE_DRAFT || $mode == rcmail_sendmail::MODE_EDIT) {
            // keep exactly what the user had typed
            $from = rcube_mime::decode_address_list($headers->from, null, true, $charset);
            $first = $from ? reset($from) : null;
            if ($first && !empty($first['mailto']) && empty($own[strtolower($first['mailto'])])) {
                $custom = $first['string'];
            }
        }
        elseif ($mode == rcmail_sendmail::MODE_REPLY || $mode == rcmail_sendmail::MODE_FORWARD) {
            $candidates = [];
            foreach (['to', 'cc'] as $field) {
                if (!empty($headers->$field)) {
                    foreach (rcube_mime::decode_address_list($headers->$field, null, true, $charset) as $addr) {
                        $candidates[] = $addr;
                    }
                }
            }
            if ($mode == rcmail_sendmail::MODE_REPLY) {
                // replying to something we sent from a custom address
                foreach (rcube_mime::decode_address_list($headers->from, null, true, $charset) as $addr) {
                    $candidates[] = $addr;
                }
            }
            $seen = [];
            foreach ($candidates as $addr) {
                $mail = strtolower($addr['mailto'] ?? '');
                if ($mail === '' || isset($seen[$mail]) || !empty($own[$mail]) || $mail === $this->owner()) {
                    continue;
                }
                $seen[$mail] = true;
                if (count($seen) > 10) {
                    break;  // bound the lookups on huge recipient lists
                }
                if ($this->hosted_domain($mail) && $this->may_send_as($mail)) {
                    $name = '';
                    $default = $args['identities'] ? reset($args['identities']) : null;
                    if ($default && !empty($default['name'])) {
                        $name = $default['name'];
                    }
                    $custom = $name !== '' ? format_email_recipient($mail, $name) : $mail;
                    break;
                }
            }
        }

        if ($custom !== null) {
            $this->rc->output->set_env('compose_from_custom', $custom);
        }
        return $args;
    }

    /** AJAX: may this mailbox send as the typed address? */
    public function action_check()
    {
        $raw = trim((string) rcube_utils::get_input_string('_address', rcube_utils::INPUT_POST, true));
        $list = rcube_mime::decode_address_list($raw, null, true, RCUBE_CHARSET);
        $first = count($list) == 1 ? reset($list) : null;
        $mail = $first ? strtolower($first['mailto'] ?? '') : '';
        if ($mail === '' || !rcube_utils::check_email($mail, false)) {
            $state = 'invalid';
        }
        else {
            $allowed = $this->may_send_as($mail);
            $state = $allowed === null ? 'unknown' : ($allowed ? 'ok' : 'denied');
        }
        $this->rc->output->command('plugin.compose_from_checked', ['address' => $raw, 'state' => $state]);
        $this->rc->output->send();
    }
}
