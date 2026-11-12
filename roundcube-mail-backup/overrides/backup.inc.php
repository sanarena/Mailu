<?php
// SPDX-License-Identifier: GPL-3.0-or-later
//
// mail_backup plugin wiring for the k8s Mailu deploy. Copied to /overrides
// by the install-backup-plugin initContainer; start.py auto-includes
// /overrides/*.inc.php AFTER the generated config.

// Guarded by directory presence: removing the plugin (rollback) leaves
// this include inert instead of breaking webmail.
if (is_dir('/var/www/roundcube/plugins/mail_backup')
    && !in_array('mail_backup', (array) $config['plugins'])) {
    $config['plugins'][] = 'mail_backup';
}

$config['backup_api_url'] = getenv('BACKUP_API_URL')
    ?: 'http://mail-backup.mail.svc.cluster.local:8080';
// php-fpm runs with clear_env=yes, so the secret comes from a file the
// initContainer renders from the live-only `backup-api` Secret (start.py
// only includes *.inc / *.inc.php, so the .key file is never included).
$backup_secret_file = '/overrides/backup-secret.key';
$config['backup_api_secret'] = is_readable($backup_secret_file)
    ? trim((string) file_get_contents($backup_secret_file))
    : (getenv('BACKUP_API_SECRET') ?: '');
