<?php

// SPDX-License-Identifier: GPL-3.0-or-later

/**
 * mail_scheduler - snooze + send-later for Roundcube, backed by the
 * mail-scheduler daemon (internal HTTP API, shared-secret auth).
 *
 * Snooze: moves selected messages to the Snoozed folder and registers
 * a wake-up; the daemon moves them back at wake time. Moving a message
 * out of Snoozed manually (or Unsnooze) cancels the wake-up.
 *
 * Send later: the compose "Send later" button schedules the send; the
 * message_before_send hook captures the MIME, registers it with the
 * daemon and aborts the live SMTP send. The copy is stored in the
 * Scheduled folder (via the standard _store_target mechanism) and the
 * daemon files it to Sent after submitting.
 *
 * Config (see overrides/scheduler.inc.php):
 *   scheduler_api_url        daemon base URL (ClusterIP, no TLS)
 *   scheduler_api_secret     shared secret (env SCHEDULER_API_SECRET)
 *   scheduler_snooze_folder  default "Snoozed"
 *   scheduler_scheduled_folder default "Scheduled"
 *
 * @license GPL-3.0-or-later
 */
class mail_scheduler extends rcube_plugin
{
    public $task = 'mail';

    /** @var rcmail */
    private $rc;

    public function init()
    {
        $this->rc = rcmail::get_instance();
        $this->load_config();

        $this->register_action('plugin.scheduler.snooze', [$this, 'action_snooze']);
        $this->register_action('plugin.scheduler.unsnooze', [$this, 'action_unsnooze']);
        $this->add_hook('message_before_send', [$this, 'message_before_send']);
        $this->add_texts('localization', true);

        if (!$this->rc->output || $this->rc->output->type != 'html') {
            return;
        }

        $this->include_script('mail_scheduler.js');
        $this->include_stylesheet($this->local_skin_path() . '/mail_scheduler.css');

        $action = $this->rc->action;

        if ($action == '' || $action == 'show') {
            $this->add_button([
                'command'    => 'plugin.scheduler.snooze',
                'type'       => 'link',
                'class'      => 'button buttonPas scheduler-snooze disabled',
                'classact'   => 'button scheduler-snooze',
                'classsel'   => 'button scheduler-snooze pressed',
                'title'      => 'mail_scheduler.snooze',
                'innerclass' => 'inner',
                'label'      => 'mail_scheduler.snooze',
            ], 'toolbar');
            $this->add_button([
                'command'    => 'plugin.scheduler.unsnooze',
                'type'       => 'link',
                'class'      => 'button buttonPas scheduler-unsnooze disabled',
                'classact'   => 'button scheduler-unsnooze',
                'classsel'   => 'button scheduler-unsnooze pressed',
                'title'      => 'mail_scheduler.unsnooze',
                'innerclass' => 'inner',
                'label'      => 'mail_scheduler.unsnooze',
            ], 'toolbar');
            $this->rc->output->set_env('scheduler_snooze_folder',
                $this->rc->config->get('scheduler_snooze_folder', 'Snoozed'));
        }
        elseif ($action == 'compose') {
            $this->add_button([
                'command'    => 'plugin.scheduler.sendlater',
                'type'       => 'link',
                'class'      => 'button scheduler-sendlater',
                'classact'   => 'button scheduler-sendlater',
                'title'      => 'mail_scheduler.sendlater',
                'innerclass' => 'inner',
                'label'      => 'mail_scheduler.sendlater',
            ], 'toolbar');
        }
    }

    // -- helpers ----------------------------------------------------

    private function owner()
    {
        return strtolower(trim((string) $this->rc->user->get_username()));
    }

    private function api($method, $path, $data = null)
    {
        $url = rtrim((string) $this->rc->config->get('scheduler_api_url'), '/') . $path;
        $secret = (string) $this->rc->config->get('scheduler_api_secret');
        $ch = curl_init($url);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, 20);
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        $headers = ['X-Scheduler-Secret: ' . $secret];
        if ($data !== null) {
            $payload = json_encode($data);
            $headers[] = 'Content-Type: application/json';
            curl_setopt($ch, CURLOPT_POSTFIELDS, $payload);
        }
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        $body = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err = curl_error($ch);
        curl_close($ch);
        if ($body === false || $code < 200 || $code >= 300) {
            rcube::raise_error([
                'code' => 600, 'file' => __FILE__, 'line' => __LINE__,
                'message' => "scheduler API $method $path failed"
                    . " (http $code): $err " . substr((string) $body, 0, 200),
            ], true, false);
            return null;
        }
        return json_decode((string) $body, true);
    }

    private static function valid_epoch($value)
    {
        $epoch = (int) $value;
        $now = time();
        if ($epoch < $now + 30 || $epoch > $now + 366 * 86400) {
            return 0;
        }
        return $epoch;
    }

    private static function bare_address($addr)
    {
        if (preg_match('/<([^<>\s]+@[^<>\s]+)>/', (string) $addr, $m)) {
            return strtolower(trim($m[1]));
        }
        return strtolower(trim((string) $addr));
    }

    private function ensure_folder($folder)
    {
        $storage = $this->rc->get_storage();
        if ($storage->folder_exists($folder, true)) {
            return true;
        }
        if (!$storage->folder_exists($folder)) {
            return (bool) $storage->create_folder($folder, true);
        }
        return (bool) $storage->subscribe($folder);
    }

    private function message_id_of($uid, $mbox)
    {
        try {
            $msg = new rcube_message($uid, $mbox);
        }
        catch (Exception $e) {
            return '';
        }
        $mid = $msg->get_header('message-id');
        if (is_array($mid)) {
            $mid = reset($mid);
        }
        $mid = trim((string) $mid);
        if ($mid === '' || strpbrk($mid, "\r\n") !== false) {
            return '';
        }
        return $mid;
    }

    // -- snooze actions ----------------------------------------------

    public function action_snooze()
    {
        $uids = rcube_utils::get_input_string('_uid', rcube_utils::INPUT_POST);
        $mbox = rcube_utils::get_input_string('_mbox', rcube_utils::INPUT_POST);
        $wake = self::valid_epoch(
            rcube_utils::get_input_string('_wake', rcube_utils::INPUT_POST));
        $snooze_folder = $this->rc->config->get('scheduler_snooze_folder', 'Snoozed');
        $user = $this->owner();

        if ($uids === '' || $mbox === '' || !$wake || $user === ''
            || strcasecmp($mbox, $snooze_folder) === 0) {
            $this->rc->output->show_message('mail_scheduler.invalid', 'error');
            $this->rc->output->send();
            return;
        }

        if (!$this->ensure_folder($snooze_folder)) {
            $this->rc->output->show_message('mail_scheduler.foldererror', 'error');
            $this->rc->output->send();
            return;
        }

        $done = [];
        foreach (array_filter(explode(',', $uids)) as $uid) {
            $mid = $this->message_id_of($uid, $mbox);
            if ($mid === '') {
                continue;
            }
            $res = $this->api('POST', '/v1/snoozes', [
                'user' => $user, 'message_id' => $mid,
                'folder' => $mbox, 'wake_at' => $wake,
            ]);
            if (is_array($res) && isset($res['id'])) {
                $done[] = $uid;
            }
        }

        if ($done) {
            $this->rc->get_storage()->move_message($done, $snooze_folder, $mbox);
            $this->rc->output->show_message('mail_scheduler.snoozed', 'confirmation');
        }
        else {
            $this->rc->output->show_message('mail_scheduler.schedulererror', 'error');
        }
        $this->rc->output->command('command', 'list', $mbox);
        $this->rc->output->send();
    }

    public function action_unsnooze()
    {
        $uids = rcube_utils::get_input_string('_uid', rcube_utils::INPUT_POST);
        $mbox = rcube_utils::get_input_string('_mbox', rcube_utils::INPUT_POST);
        $user = $this->owner();

        if ($uids === '' || $mbox === '' || $user === '') {
            $this->rc->output->show_message('mail_scheduler.invalid', 'error');
            $this->rc->output->send();
            return;
        }

        $res = $this->api('GET', '/v1/snoozes?user=' . urlencode($user));
        $by_mid = [];
        if (is_array($res) && isset($res['snoozes'])) {
            foreach ($res['snoozes'] as $row) {
                if (($row['status'] ?? '') === 'pending') {
                    $by_mid[$row['message_id']] = $row;
                }
            }
        }

        $storage = $this->rc->get_storage();
        $moved = 0;
        $targets = [];
        foreach (array_filter(explode(',', $uids)) as $uid) {
            $mid = $this->message_id_of($uid, $mbox);
            $dest = 'INBOX';
            if ($mid !== '' && isset($by_mid[$mid])) {
                $dest = $by_mid[$mid]['folder'] ?: 'INBOX';
                $this->api('DELETE', '/v1/snoozes/' . (int) $by_mid[$mid]['id']
                    . '?user=' . urlencode($user));
            }
            $targets[$dest][] = $uid;
        }
        foreach ($targets as $dest => $list) {
            if (strcasecmp($dest, $mbox) !== 0
                && $storage->move_message($list, $dest, $mbox)) {
                $moved += count($list);
            }
        }

        if ($moved) {
            $this->rc->output->show_message('mail_scheduler.woken', 'confirmation');
        }
        else {
            $this->rc->output->show_message('mail_scheduler.nothingtodo', 'notice');
        }
        $this->rc->output->command('command', 'list', $mbox);
        $this->rc->output->send();
    }

    // -- send later --------------------------------------------------

    public function message_before_send($args)
    {
        $send_at = rcube_utils::get_input_string('_send_at', rcube_utils::INPUT_POST);
        if ($send_at === '') {
            return $args; // normal send, untouched
        }
        $epoch = self::valid_epoch($send_at);
        $sender = self::bare_address($args['from']);
        if (!$epoch || $sender === '' || strpos($sender, '@') === false) {
            $args['abort'] = true;
            $args['result'] = false;
            $args['error'] = $this->gettext('invalidtime');
            return $args;
        }

        /** @var Mail_Mime $message */
        $message = $args['message'];
        $headers = $message->headers();
        $recipients = array_merge(
            (array) $args['mailto'],
            [$headers['Cc'] ?? '', $headers['Bcc'] ?? '']
        );
        // stored copy must not leak Bcc (same stripping as live SMTP)
        $mime_headers = $message->txtHeaders(['Bcc' => null], true);
        if ($message->getParam('delay_file_io')) {
            $file = rcube_utils::temp_filename('sched');
            $mime_result = $message->saveMessageBody($file);
            if (is_a($mime_result, 'PEAR_Error')) {
                $args['abort'] = true;
                $args['result'] = false;
                $args['error'] = $this->gettext('schedulererror');
                return $args;
            }
            $mime_body = file_get_contents($file);
            @unlink($file);
        }
        else {
            $mime_body = $message->get();
        }
        if (!is_string($mime_body) || $mime_body === '') {
            $args['abort'] = true;
            $args['result'] = false;
            $args['error'] = $this->gettext('schedulererror');
            return $args;
        }

        $res = $this->api('POST', '/v1/scheduled', [
            'user' => $this->owner(),
            'sender' => $sender,
            'recipients' => array_values(array_filter(array_map('strval', $recipients))),
            'mime_b64' => base64_encode($mime_headers . "\r\n" . $mime_body),
            'send_at' => $epoch,
        ]);
        if (!is_array($res) || !isset($res['id'])) {
            // scheduler unreachable: abort WITHOUT losing the compose state
            $args['abort'] = true;
            $args['result'] = false;
            $args['error'] = $this->gettext('schedulererror');
            return $args;
        }

        // registered: skip live SMTP; core still files the copy into the
        // Scheduled folder via _store_target and reports success
        $args['abort'] = true;
        $args['result'] = true;
        return $args;
    }
}
