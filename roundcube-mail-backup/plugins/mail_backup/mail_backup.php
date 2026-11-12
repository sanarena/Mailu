<?php
// SPDX-License-Identifier: GPL-3.0-or-later

/**
 * mail_backup: per-mailbox IMAP backup target under Settings > Backup.
 *
 * The mail-backup daemon copies every folder of this mailbox (Sent
 * included, Junk optional) into the user's own IMAP account, append-only.
 * This plugin only edits the target and shows its status, through the
 * daemon's internal API (ClusterIP + shared secret; the owner is always
 * the logged-in user). The target password is write-only: it is sent
 * to the daemon once, stored there encrypted and never sent back.
 */
class mail_backup extends rcube_plugin
{
    public $task = 'settings';

    /** @var rcmail */
    private $rc;

    public function init()
    {
        $this->rc = rcmail::get_instance();
        $this->add_texts('localization', true);

        $this->add_hook('preferences_sections_list', [$this, 'sections_list']);
        $this->add_hook('preferences_list', [$this, 'preferences_list']);
        $this->add_hook('preferences_save', [$this, 'preferences_save']);

        $this->register_action('plugin.backup.test', [$this, 'action_test']);
        $this->register_action('plugin.backup.run', [$this, 'action_run']);
        $this->register_action('plugin.backup.status', [$this, 'action_status']);

        if ($this->rc->output && $this->rc->output->type == 'html') {
            $this->include_stylesheet($this->local_skin_path() . '/mail_backup.css');
            $this->include_script('mail_backup.js');
        }
    }

    // -- daemon API -------------------------------------------------

    private function owner()
    {
        return strtolower(trim((string) $this->rc->user->get_username()));
    }

    /** [http_code, decoded_json_or_null]; code 0 = daemon unreachable. */
    private function api($method, $path, $data = null)
    {
        $url = rtrim((string) $this->rc->config->get('backup_api_url'), '/') . $path;
        $secret = (string) $this->rc->config->get('backup_api_secret');
        $ch = curl_init($url);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        // connection tests reach out to the target server: allow its timeout
        curl_setopt($ch, CURLOPT_TIMEOUT, 75);
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        $headers = ['X-Backup-Secret: ' . $secret];
        if ($data !== null) {
            $headers[] = 'Content-Type: application/json';
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
        }
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        $body = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err = curl_error($ch);
        curl_close($ch);
        if ($body === false || $code == 0) {
            rcube::raise_error([
                'code' => 600, 'file' => __FILE__, 'line' => __LINE__,
                'message' => "backup API $method $path failed: $err",
            ], true, false);
            return [0, null];
        }
        return [$code, json_decode((string) $body, true)];
    }

    private function status()
    {
        list($code, $view) = $this->api('GET', '/v1/target?user=' . rawurlencode($this->owner()));
        return $code == 200 && is_array($view) ? $view : null;
    }

    // -- settings section -------------------------------------------

    public function sections_list($args)
    {
        $args['list']['backup'] = [
            'id'      => 'backup',
            'section' => $this->gettext('backup'),
        ];
        return $args;
    }

    public function preferences_list($args)
    {
        if ($args['section'] != 'backup') {
            return $args;
        }

        $view = $this->status();
        $configured = $view && !empty($view['configured']);
        $val = function ($key, $default = '') use ($view, $configured) {
            return $configured && isset($view[$key]) ? $view[$key] : $default;
        };

        $args['blocks']['intro'] = [
            'name'    => rcube::Q($this->gettext('backup')),
            'options' => [
                'intro' => [
                    'content' => html::p('backup-intro', rcube::Q($this->gettext('intro'))),
                ],
            ],
        ];

        if ($view === null) {
            $args['blocks']['intro']['options']['down'] = [
                'content' => html::p('backup-error', rcube::Q($this->gettext('serviceunavailable'))),
            ];
            return $args;
        }

        $fields = [];
        $enabled = new html_checkbox(['name' => '_backup_enabled', 'id' => 'rcmfd_backup_enabled', 'value' => 1]);
        $fields['enabled'] = [
            'title'   => html::label('rcmfd_backup_enabled', rcube::Q($this->gettext('enabled'))),
            'content' => $enabled->show($configured ? ($view['enabled'] ? 1 : 0) : 1),
        ];
        $host = new html_inputfield(['name' => '_backup_host', 'id' => 'rcmfd_backup_host',
            'size' => 40, 'autocomplete' => 'off', 'placeholder' => 'imap.gmail.com']);
        $fields['host'] = [
            'title'   => html::label('rcmfd_backup_host', rcube::Q($this->gettext('host'))),
            'content' => $host->show($val('host')),
        ];
        $port = new html_inputfield(['name' => '_backup_port', 'id' => 'rcmfd_backup_port',
            'size' => 6, 'type' => 'number', 'min' => 1, 'max' => 65535]);
        $fields['port'] = [
            'title'   => html::label('rcmfd_backup_port', rcube::Q($this->gettext('port'))),
            'content' => $port->show($val('port', 993))
                . html::span('backup-hint', rcube::Q($this->gettext('porthint'))),
        ];
        $user = new html_inputfield(['name' => '_backup_username', 'id' => 'rcmfd_backup_username',
            'size' => 40, 'autocomplete' => 'off', 'placeholder' => 'you@gmail.com']);
        $fields['username'] = [
            'title'   => html::label('rcmfd_backup_username', rcube::Q($this->gettext('username'))),
            'content' => $user->show($val('username')),
        ];
        $pass = new html_passwordfield(['name' => '_backup_password', 'id' => 'rcmfd_backup_password',
            'size' => 40, 'autocomplete' => 'new-password',
            'placeholder' => $configured && !empty($view['has_password'])
                ? $this->gettext('passwordkept') : $this->gettext('passwordhint')]);
        $fields['password'] = [
            'title'   => html::label('rcmfd_backup_password', rcube::Q($this->gettext('password'))),
            'content' => $pass->show(),
        ];
        $prefix = new html_inputfield(['name' => '_backup_prefix', 'id' => 'rcmfd_backup_prefix',
            'size' => 40, 'autocomplete' => 'off']);
        $fields['prefix'] = [
            'title'   => html::label('rcmfd_backup_prefix', rcube::Q($this->gettext('prefix'))),
            'content' => $prefix->show($val('prefix', 'Backup/' . $this->owner()))
                . html::span('backup-hint', rcube::Q($this->gettext('prefixhint'))),
        ];
        $junk = new html_checkbox(['name' => '_backup_include_junk', 'id' => 'rcmfd_backup_include_junk', 'value' => 1]);
        $fields['include_junk'] = [
            'title'   => html::label('rcmfd_backup_include_junk', rcube::Q($this->gettext('includejunk'))),
            'content' => $junk->show(!empty($view['include_junk']) ? 1 : 0),
        ];
        $fields['actions'] = [
            'title'   => '',
            'content' => html::div('backup-actions',
                html::tag('button', ['type' => 'button', 'id' => 'backup-test',
                    'class' => 'btn btn-secondary'], rcube::Q($this->gettext('test')))
                . ($configured ? ' ' . html::tag('button', ['type' => 'button', 'id' => 'backup-run',
                    'class' => 'btn btn-secondary'], rcube::Q($this->gettext('runnow'))) : '')
                . html::span(['id' => 'backup-result', 'class' => 'backup-result', 'aria-live' => 'polite'], '')),
        ];
        $args['blocks']['target'] = [
            'name'    => rcube::Q($this->gettext('target')),
            'options' => $fields,
        ];

        if ($configured) {
            $args['blocks']['status'] = [
                'name'    => rcube::Q($this->gettext('status')),
                'options' => $this->status_rows($view),
            ];
        }

        $this->rc->output->set_env('backup_configured', $configured);
        $this->rc->output->set_env('backup_busy', $configured && (!empty($view['running']) || !empty($view['queued'])));

        return $args;
    }

    private function status_rows($view)
    {
        $fmt = function ($ts) {
            return $ts ? $this->rc->format_date((int) $ts) : $this->gettext('never');
        };
        if (!empty($view['running'])) {
            $state = $this->gettext('staterunning');
        } elseif (!empty($view['queued'])) {
            $state = $this->gettext('statequeued');
        } elseif (empty($view['enabled'])) {
            $state = $this->gettext('stateoff');
        } elseif (!empty($view['last_error']) && $view['last_ok_at'] != $view['last_run_at']) {
            $state = $this->gettext('stateerror');
        } elseif (!empty($view['backlog'])) {
            $state = $this->gettext('statebacklog');
        } else {
            $state = $view['last_ok_at'] ? $this->gettext('stateok') : $this->gettext('statewaiting');
        }
        $rows = [
            'state'  => ['title' => rcube::Q($this->gettext('state')),
                         'content' => html::span(['id' => 'backup-state'], rcube::Q($state))],
            'last'   => ['title' => rcube::Q($this->gettext('lastok')),
                         'content' => html::span(['id' => 'backup-last'], rcube::Q($fmt($view['last_ok_at'])))],
            'copied' => ['title' => rcube::Q($this->gettext('copied')),
                         'content' => html::span(['id' => 'backup-copied'], rcube::Q((string) (int) $view['copied_total']))],
        ];
        if (!empty($view['backlog'])) {
            $rows['backlog'] = ['title' => rcube::Q($this->gettext('backlog')),
                                'content' => rcube::Q($this->gettext(['name' => 'backlogn',
                                    'vars' => ['n' => (int) $view['backlog']]]))];
        }
        if (!empty($view['last_error'])) {
            $rows['error'] = ['title' => rcube::Q($this->gettext('lasterror')),
                              'content' => html::span('backup-error', rcube::Q($view['last_error']))];
        }
        return $rows;
    }

    private function posted()
    {
        $get = function ($name) {
            return trim((string) rcube_utils::get_input_string($name, rcube_utils::INPUT_POST));
        };
        return [
            'user'         => $this->owner(),
            'enabled'      => !empty($_POST['_backup_enabled']),
            'host'         => $get('_backup_host'),
            'port'         => (int) $get('_backup_port') ?: 993,
            'username'     => $get('_backup_username'),
            // not trimmed: app passwords may contain spaces
            'password'     => (string) rcube_utils::get_input_string('_backup_password', rcube_utils::INPUT_POST, true),
            'prefix'       => $get('_backup_prefix'),
            'include_junk' => !empty($_POST['_backup_include_junk']),
        ];
    }

    public function preferences_save($args)
    {
        if ($args['section'] != 'backup') {
            return $args;
        }
        $data = $this->posted();
        $current = $this->status();
        if ($current === null) {
            $args['abort'] = true;
            $args['message'] = $this->gettext('serviceunavailable');
            return $args;
        }
        // An empty server name removes the backup target.
        if ($data['host'] === '') {
            if (!empty($current['configured'])) {
                list($code, ) = $this->api('DELETE', '/v1/target?user=' . rawurlencode($this->owner()));
                if ($code != 200) {
                    $args['abort'] = true;
                    $args['message'] = $this->gettext('serviceunavailable');
                }
            }
            return $args;
        }
        if ($data['password'] === '') {
            unset($data['password']);  // keep the stored one
        }
        list($code, $resp) = $this->api('PUT', '/v1/target', $data);
        if ($code != 200) {
            $args['abort'] = true;
            $args['message'] = $this->gettext('saveerror') . ': '
                . (is_array($resp) && !empty($resp['error']) ? $resp['error'] : $this->gettext('serviceunavailable'));
        }
        return $args;
    }

    // -- AJAX actions -----------------------------------------------

    public function action_test()
    {
        $get = function ($name) {
            return trim((string) rcube_utils::get_input_string($name, rcube_utils::INPUT_POST));
        };
        $data = [
            'user'     => $this->owner(),
            'host'     => $get('_host'),
            'port'     => (int) $get('_port') ?: 993,
            'username' => $get('_username'),
        ];
        $password = (string) rcube_utils::get_input_string('_password', rcube_utils::INPUT_POST, true);
        if ($password !== '') {
            $data['password'] = $password;
        }
        list($code, $resp) = $this->api('POST', '/v1/test', $data);
        if ($code == 0 || !is_array($resp)) {
            $result = ['ok' => false, 'message' => $this->gettext('serviceunavailable')];
        } elseif ($code == 429) {
            $result = ['ok' => false, 'message' => $this->gettext('slowdown')];
        } else {
            $result = ['ok' => !empty($resp['ok']),
                       'message' => (string) ($resp['message'] ?? ($resp['error'] ?? ''))];
        }
        $this->rc->output->command('plugin.backup_result', $result);
        $this->rc->output->send();
    }

    public function action_run()
    {
        list($code, $resp) = $this->api('POST', '/v1/run', ['user' => $this->owner()]);
        if ($code == 200) {
            $result = ['ok' => true, 'message' => $this->gettext('runqueued'), 'poll' => true];
        } else {
            $result = ['ok' => false, 'message' => is_array($resp) && !empty($resp['error'])
                ? $resp['error'] : $this->gettext('serviceunavailable')];
        }
        $this->rc->output->command('plugin.backup_result', $result);
        $this->rc->output->send();
    }

    public function action_status()
    {
        $view = $this->status();
        $out = ['ok' => $view !== null];
        if ($view && !empty($view['configured'])) {
            $rows = $this->status_rows($view);
            $out['busy'] = !empty($view['running']) || !empty($view['queued']);
            $out['state'] = html_entity_decode(strip_tags($rows['state']['content']), ENT_QUOTES, 'UTF-8');
            $out['last'] = html_entity_decode(strip_tags($rows['last']['content']), ENT_QUOTES, 'UTF-8');
            $out['copied'] = (int) $view['copied_total'];
            $out['error'] = (string) $view['last_error'];
        }
        $this->rc->output->command('plugin.backup_status', $out);
        $this->rc->output->send();
    }
}
