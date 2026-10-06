<?php
// SPDX-License-Identifier: GPL-3.0-or-later
//
// mail_scheduler plugin wiring for the k8s Mailu deploy. Mounted at
// /overrides by the scheduler-pkg initContainer; start.py auto-includes
// /overrides/*.inc.php AFTER the generated config.

// Guarded by directory presence: removing the plugin mount (rollback)
// leaves this include inert instead of breaking webmail.
if (is_dir('/var/www/roundcube/plugins/mail_scheduler')
    && !in_array('mail_scheduler', (array) $config['plugins'])) {
    $config['plugins'][] = 'mail_scheduler';
}

$config['scheduler_api_url'] = getenv('SCHEDULER_API_URL')
    ?: 'http://mail-scheduler.mail.svc.cluster.local:8080';
$config['scheduler_api_secret'] = getenv('SCHEDULER_API_SECRET') ?: '';
$config['scheduler_snooze_folder'] = 'Snoozed';
$config['scheduler_scheduled_folder'] = 'Scheduled';
