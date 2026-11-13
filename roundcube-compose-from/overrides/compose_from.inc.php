<?php
// SPDX-License-Identifier: GPL-3.0-or-later
//
// compose_from plugin wiring for the k8s Mailu deploy. Copied to /overrides
// by the install-compose-from initContainer; start.py auto-includes
// /overrides/*.inc.php AFTER the generated config.

// Guarded by directory presence: removing the plugin (rollback) leaves
// this include inert instead of breaking webmail.
if (is_dir('/var/www/roundcube/plugins/compose_from')
    && !in_array('compose_from', (array) $config['plugins'])) {
    $config['plugins'][] = 'compose_from';
}

// Mailu admin, for the sender-login / hosted-domain lookups postfix uses
// (internal endpoints, reachable on the pod network only).
$config['compose_from_admin_url'] = getenv('COMPOSE_FROM_ADMIN_URL')
    ?: 'http://mail-mailu-admin.mail.svc.cluster.local:8080';
