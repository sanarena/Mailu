<?php
// SPDX-License-Identifier: GPL-3.0-or-later
//
// mail_tracking plugin wiring for the k8s Mailu deploy. Mounted at
// /overrides by the tracking-pkg initContainer; start.py auto-includes
// /overrides/*.inc.php AFTER the generated config.

// Guarded by directory presence: removing the plugin mount (rollback)
// leaves this include inert instead of breaking webmail.
if (is_dir('/var/www/roundcube/plugins/mail_tracking')
    && !in_array('mail_tracking', (array) $config['plugins'])) {
    $config['plugins'][] = 'mail_tracking';
}

$config['tracking_api_url'] = getenv('TRACKING_API_URL')
    ?: 'http://mail-tracker.mail.svc.cluster.local:8080';
// NOTE: php-fpm runs with clear_env=yes, so getenv() NEVER sees container
// env here. The initContainer renders the live secret to
// /overrides/tracking-secret.key (any charset safe; start.py only
// includes *.inc / *.inc.php, so the raw key file is never included).
// getenv() remains as a fallback for non-fpm runtimes (cli/dev).
$tracking_secret_file = '/overrides/tracking-secret.key';
$config['tracking_api_secret'] = is_readable($tracking_secret_file)
    ? trim((string) file_get_contents($tracking_secret_file))
    : (getenv('TRACKING_API_SECRET') ?: '');
