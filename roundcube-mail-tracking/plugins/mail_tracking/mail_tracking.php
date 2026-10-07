<?php
// SPDX-License-Identifier: GPL-3.0-or-later
//
// mail_tracking: Mailbutler-like tracking suite inside Roundcube.
//
// - Sent list subjects get tracking badges (opened/clicked).
// - Sent message view gets a Tracking header row with a Details link.
// - A dedicated Tracking task shows stats, sent messages with event
//   timelines, and opt-out management.
// - Compose gets a "track opens and clicks" checkbox (default on);
//   unchecking sets X-Tracker-Track: no (the milter default-tracks).
//
// Data comes from the mail-tracker daemon over its internal API with
// service auth (shared secret + asserted user, owner-scoped inside).
// The plugin never sees mailbox passwords.
//
// Config (overrides/tracking.inc.php):
//   tracking_api_url           daemon base URL (ClusterIP, no TLS)
//   tracking_api_secret        shared secret (file, see overrides)

class mail_tracking extends rcube_plugin
{
    public $task = 'mail|tracking';

    private $rc;
    private $batch_cache = [];

    public function init()
    {
        $this->rc = rcmail::get_instance();
        $this->load_config();
        $this->add_texts('localization', true);
        $this->register_task('tracking');

        $task = $this->rc->task;
        if ($task == 'mail') {
            $this->include_script('mail_tracking.js');
            $this->include_stylesheet('skins/elastic/mail_tracking.css');
            $this->add_hook('messages_list', [$this, 'hook_messages_list']);
            $this->add_hook('message_headers_output', [$this, 'hook_headers']);
            // Header must exist before mail_scheduler captures the MIME
            // (message_before_send) regardless of plugin load order, and
            // again at final send (draft round-trips rebuild the MIME).
            $this->add_hook('message_ready', [$this, 'hook_track_header']);
            $this->add_hook('message_before_send', [$this, 'hook_track_header']);
        }
        elseif ($task == 'tracking') {
            $this->include_script('mail_tracking.js');
            $this->include_stylesheet('skins/elastic/mail_tracking.css');
            $this->register_action('index', [$this, 'action_index']);
            $this->register_action('stats', [$this, 'action_stats']);
            $this->register_action('messages', [$this, 'action_messages']);
            $this->register_action('detail', [$this, 'action_detail']);
            $this->register_action('set_tracking', [$this, 'action_set_tracking']);
            $this->register_action('optouts', [$this, 'action_optouts']);
        }

        if ($this->rc->output->type == 'html') {
            $this->add_button([
                'command' => 'tracking',
                'type' => 'link',
                'label' => 'mail_tracking.tracking',
                'title' => 'mail_tracking.trackingtitle',
                'class' => 'tracking',
                'innerclass' => 'inner',
            ], 'taskbar');
        }
    }

    /* -- tracker API ------------------------------------------------- */

    private function api($method, $path, array $payload = null, $user = null)
    {
        $url = rtrim((string) $this->rc->config->get('tracking_api_url'), '/') . $path;
        $secret = (string) $this->rc->config->get('tracking_api_secret');
        $ch = curl_init($url);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, 15);
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        $body = null;
        if ($payload !== null) {
            $body = json_encode($payload);
            curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
            curl_setopt($ch, CURLOPT_HTTPHEADER, [
                'X-Tracker-Secret: ' . $secret,
                'Content-Type: application/json',
            ]);
        }
        else {
            curl_setopt($ch, CURLOPT_HTTPHEADER, [
                'X-Tracker-Secret: ' . $secret,
            ]);
        }
        $raw = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err = curl_error($ch);
        curl_close($ch);
        if ($raw === false || $code < 200 || $code >= 300) {
            rcube::raise_error([
                'code' => 520, 'file' => __FILE__, 'line' => __LINE__,
                'message' => "tracker API $method $path failed"
                    . " (http $code): $err " . substr((string) $raw, 0, 200),
            ], true, false);
            return null;
        }
        return json_decode($raw, true);
    }

    private function owner()
    {
        $user = $this->rc->get_user_name();
        return $user ? strtolower(trim((string) $user)) : '';
    }

    private function is_sent_folder($folder)
    {
        $sent = (string) $this->rc->config->get('sent_mbox', 'Sent');
        return strcasecmp(trim((string) $folder), $sent) === 0;
    }

    private function real_mid($mid)
    {
        // List/view headers carry synthetic 'mid:<seq>' because the index
        // fetch does not include MESSAGE-ID; it matches nothing
        // server-side, so never look it up.
        $mid = trim((string) $mid);
        if ($mid === '' || stripos($mid, 'mid:') === 0) {
            return '';
        }
        return $mid;
    }

    /**
     * Resolve real Message-IDs for UIDs with one bulk IMAP FETCH.
     * Returns [uid => mid]; failures yield an empty map, never an error.
     */
    private function fetch_mids($folder, array $uids)
    {
        $map = [];
        $uids = array_values(array_unique(array_filter(
            array_map('intval', $uids))));
        if (!$uids || !is_string($folder) || $folder === '') {
            return $map;
        }
        try {
            $storage = $this->rc->get_storage();
            if (!is_object($storage) || !isset($storage->conn)
                || !is_object($storage->conn)
            ) {
                return $map;
            }
            $res = $storage->conn->fetch($folder, $uids, true,
                ['UID', 'BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]']);
        }
        catch (Exception $e) {
            return $map;
        }
        if (!is_array($res)) {
            return $map;
        }
        foreach ($res as $header) {
            if (isset($header->uid) && isset($header->messageID)) {
                $mid = $this->real_mid($header->messageID);
                if ($mid !== '') {
                    $map[(int) $header->uid] = $mid;
                }
            }
        }
        return $map;
    }

    /** Batch lookup with per-request cache. Returns [mid => summary]. */
    private function summaries(array $mids)
    {
        $mids = array_values(array_unique(array_filter(array_map(
            [$this, 'real_mid'], $mids))));
        if (!$mids) {
            return [];
        }
        $missing = [];
        foreach ($mids as $mid) {
            if (!array_key_exists($mid, $this->batch_cache)) {
                $missing[] = $mid;
            }
        }
        if ($missing) {
            $res = $this->api('POST', '/api/by-mids', [
                'user' => $this->owner(), 'message_ids' => $missing,
            ]);
            $rows = (is_array($res) && isset($res['results'])
                && is_array($res['results'])) ? $res['results'] : [];
            foreach ($missing as $mid) {
                $this->batch_cache[$mid] = isset($rows[$mid]) ? $rows[$mid] : null;
            }
        }
        $out = [];
        foreach ($mids as $mid) {
            if (!empty($this->batch_cache[$mid])) {
                $out[$mid] = $this->batch_cache[$mid];
            }
        }
        return $out;
    }

    /* -- mail list badges -------------------------------------------- */

    public function hook_messages_list($args)
    {
        $mbox = '';
        try {
            $mbox = (string) $this->rc->get_storage()->get_folder();
        }
        catch (Exception $e) {
            return $args;
        }
        if (!$this->is_sent_folder($mbox)) {
            return $args;
        }
        // Index headers only carry synthetic 'mid:<seq>' IDs, so resolve
        // the real Message-IDs for this page with one bulk FETCH.
        $uids = [];
        foreach ((array) $args['messages'] as $key => $header) {
            $uid = isset($header->uid) ? (int) $header->uid
                : (is_numeric($key) ? (int) $key : 0);
            if ($uid > 0) {
                $uids[$key] = $uid;
            }
        }
        if (!$uids) {
            return $args;
        }
        $mid_by_uid = $this->fetch_mids($mbox, array_values($uids));
        if (!$mid_by_uid) {
            return $args;
        }
        $rows = $this->summaries(
            array_slice(array_values($mid_by_uid), 0, 200));
        // No separate column: widescreen (the default layout) renders a
        // hardcoded column set, so the badge rides on the subject cell.
        foreach ((array) $args['messages'] as $key => $header) {
            $mid = (isset($uids[$key]) && isset($mid_by_uid[$uids[$key]]))
                ? $mid_by_uid[$uids[$key]] : '';
            $row = ($mid !== '' && isset($rows[$mid])) ? $rows[$mid] : null;
            $badge = $this->badge_html($row);
            if ($badge === '') {
                continue;
            }
            // Same formatting as core's subject column, plus the badge.
            $subject = trim(rcube_mime::decode_header(
                $header->subject, $header->charset));
            if (!$subject) {
                $subject = $this->rc->gettext('nosubject');
            }
            if (!is_array($header->list_cols)) {
                $header->list_cols = [];
            }
            // Badge first: subjects truncate, so trailing badges
            // would be invisible.
            $header->list_cols['subject'] = $badge . ' '
                . rcube::SQ($subject);
        }
        return $args;
    }

    private function badge_html($row)
    {
        if (!is_array($row)) {
            return '';
        }
        $opens = (int) ($row['opens'] ?? 0);
        $clicks = (int) ($row['clicks'] ?? 0);
        if ($opens === 0 && $clicks === 0) {
            return '<span class="tracking-badge muted" title="'
                . rcube::Q($this->gettext('notopened')) . '">'
                . rcube::Q($this->gettext('unopened')) . '</span>';
        }
        $title = $this->gettext(['name' => 'badgehint',
            'vars' => ['opens' => $opens, 'clicks' => $clicks]]);
        $text = $this->gettext(['name' => 'openedn',
            'vars' => ['n' => $opens]]);
        if ($clicks > 0) {
            $text .= ' · ' . $this->gettext(['name' => 'clickedn',
                'vars' => ['n' => $clicks]]);
        }
        return '<span class="tracking-badge" title="' . rcube::Q($title) . '">'
            . rcube::Q($text) . '</span>';
    }

    /* -- message view row -------------------------------------------- */

    public function hook_headers($args)
    {
        $headers = $args['headers'] ?? null;
        $folder = (string) ($args['folder']
            ?? (is_object($headers) ? ($headers->folder ?? '') : ''));
        if (!$this->is_sent_folder($folder)) {
            return $args;
        }
        $mid = '';
        if (is_object($headers) && isset($headers->messageID)) {
            $mid = $this->real_mid($headers->messageID);
        }
        if ($mid === '' && is_object($headers) && !empty($headers->uid)) {
            // Cached/view headers may only carry synthetic 'mid:<seq>'.
            $map = $this->fetch_mids($folder, [(int) $headers->uid]);
            $mid = $map[(int) $headers->uid] ?? '';
        }
        if ($mid === '') {
            return $args;
        }
        $rows = $this->summaries([$mid]);
        if (!isset($rows[$mid])) {
            return $args;
        }
        $row = $rows[$mid];
        $opens = (int) ($row['opens'] ?? 0);
        $clicks = (int) ($row['clicks'] ?? 0);
        $detail = $this->rc->url(['_task' => 'tracking', '_id' => (int) $row['id']]);
        $value = rcube::Q($this->gettext(['name' => 'summaryline',
            'vars' => ['opens' => $opens, 'clicks' => $clicks]]))
            . ' <a href="' . rcube::Q($detail) . '">'
            . rcube::Q($this->gettext('details')) . '</a>';
        if (empty($row['tracking'])) {
            $value .= ' <span class="tracking-off">('
                . rcube::Q($this->gettext('trackingoff')) . ')</span>';
        }
        $args['output']['Tracking'] = [
            'title' => rcube::Q($this->gettext('tracking')),
            'value' => $value,
            'html' => true,
        ];
        return $args;
    }

    /* -- compose toggle ---------------------------------------------- */

    public function hook_track_header($args)
    {
        $track = rcube_utils::get_input_string('_track', rcube_utils::INPUT_POST);
        if ($track === '0' && isset($args['message']) && is_object($args['message'])) {
            // Opt out of the milter's default-on tracking for this mail.
            $args['message']->headers(['X-Tracker-Track' => 'no'], true);
        }
        return $args;
    }

    /* -- tracking task ----------------------------------------------- */

    public function action_index()
    {
        $open = (int) rcube_utils::get_input_value('_id', rcube_utils::INPUT_GPC);
        if ($open > 0) {
            $this->rc->output->set_env('tracking_open_id', $open);
        }
        $this->rc->output->set_pagetitle($this->gettext('tracking'));
        $this->rc->output->send('mail_tracking.tracking');
    }

    private function json_out($event, $data)
    {
        $this->rc->output->command($event, $data);
        $this->rc->output->send();
    }

    public function action_stats()
    {
        $res = $this->api('GET', '/api/stats?user=' . rawurlencode($this->owner()));
        $this->json_out('plugin.tracking.stats', is_array($res) ? $res : ['error' => true]);
    }

    public function action_messages()
    {
        $limit = (int) rcube_utils::get_input_value('limit', rcube_utils::INPUT_GPC);
        $offset = (int) rcube_utils::get_input_value('offset', rcube_utils::INPUT_GPC);
        $limit = max(1, min(100, $limit > 0 ? $limit : 50));
        $offset = max(0, $offset);
        $q = http_build_query([
            'user' => $this->owner(), 'limit' => $limit, 'offset' => $offset,
        ]);
        $res = $this->api('GET', '/api/messages?' . $q);
        $this->json_out('plugin.tracking.messages', is_array($res) ? $res : ['error' => true]);
    }

    public function action_detail()
    {
        $id = (int) rcube_utils::get_input_value('id', rcube_utils::INPUT_GPC);
        if ($id <= 0) {
            $this->json_out('plugin.tracking.detail', ['error' => true]);
            return;
        }
        $res = $this->api('GET', '/api/messages/' . $id
            . '?user=' . rawurlencode($this->owner()));
        $this->json_out('plugin.tracking.detail', is_array($res) ? $res : ['error' => true]);
    }

    public function action_set_tracking()
    {
        $id = (int) rcube_utils::get_input_value('id', rcube_utils::INPUT_GPC);
        $on = rcube_utils::get_input_value('tracking', rcube_utils::INPUT_GPC);
        if ($id <= 0 || ($on !== '1' && $on !== '0')) {
            $this->json_out('plugin.tracking.set', ['error' => true]);
            return;
        }
        $res = $this->api('POST', '/api/messages/' . $id . '/tracking', [
            'user' => $this->owner(), 'tracking' => $on === '1',
        ]);
        $this->json_out('plugin.tracking.set', is_array($res) ? $res : ['error' => true]);
    }

    public function action_optouts()
    {
        $method = strtoupper((string) rcube_utils::get_input_value('_m', rcube_utils::INPUT_GPC));
        if ($method === 'POST') {
            $scope = rcube_utils::get_input_string('scope', rcube_utils::INPUT_GPC);
            $address = strtolower(trim((string) rcube_utils::get_input_string(
                'address', rcube_utils::INPUT_GPC)));
            // sender scope without address means self (server defaults it)
            if ($address === '' && $scope === 'sender') {
                $address = $this->owner();
            }
            $off = rcube_utils::get_input_value('opted_out', rcube_utils::INPUT_GPC);
            if (($scope !== 'sender' && $scope !== 'recipient')
                || strpos($address, '@') === false
                || ($off !== '1' && $off !== '0')) {
                $this->json_out('plugin.tracking.optouts', ['error' => true]);
                return;
            }
            $res = $this->api('POST', '/api/optouts', [
                'user' => $this->owner(), 'scope' => $scope,
                'address' => $address, 'opted_out' => $off === '1',
            ]);
        }
        else {
            $res = $this->api('GET', '/api/optouts?user=' . rawurlencode($this->owner()));
        }
        $this->json_out('plugin.tracking.optouts', is_array($res) ? $res : ['error' => true]);
    }
}
