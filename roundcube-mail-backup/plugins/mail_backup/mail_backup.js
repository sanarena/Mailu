/* mail_backup: Test connection / Back up now buttons on Settings > Backup.
 * Server strings are written with textContent only. */
(function() {
  if (typeof rcmail === 'undefined') {
    return;
  }

  var pollTimer = null;
  var pollsLeft = 0;

  function el(id) {
    return document.getElementById(id);
  }

  function show(ok, message) {
    var out = el('backup-result');
    if (!out) {
      return;
    }
    out.textContent = message || '';
    out.className = 'backup-result ' + (ok ? 'ok' : 'fail');
  }

  function value(id) {
    var input = el(id);
    return input ? input.value : '';
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (pollsLeft-- > 0) {
      pollTimer = setTimeout(function() {
        rcmail.http_post('plugin.backup.status', {}, false);
      }, 4000);
    }
  }

  rcmail.addEventListener('init', function() {
    var test = el('backup-test');
    if (!test) {
      return;  // not the Backup section
    }
    test.addEventListener('click', function() {
      show(true, rcmail.gettext('testing', 'mail_backup'));
      rcmail.http_post('plugin.backup.test', {
        _host: value('rcmfd_backup_host'),
        _port: value('rcmfd_backup_port'),
        _username: value('rcmfd_backup_username'),
        _password: value('rcmfd_backup_password')
      }, rcmail.set_busy(true, 'loading'));
    });
    var run = el('backup-run');
    if (run) {
      run.addEventListener('click', function() {
        rcmail.http_post('plugin.backup.run', {}, rcmail.set_busy(true, 'loading'));
      });
    }
    if (rcmail.env.backup_busy) {
      pollsLeft = 30;
      schedulePoll();
    }
  });

  rcmail.addEventListener('plugin.backup_result', function(res) {
    show(res && res.ok, res && res.message);
    if (res && res.poll) {
      pollsLeft = 30;
      schedulePoll();
    }
  });

  rcmail.addEventListener('plugin.backup_status', function(res) {
    if (!res || !res.ok) {
      return;
    }
    var fields = {'backup-state': res.state, 'backup-last': res.last,
      'backup-copied': res.copied === undefined ? '' : String(res.copied)};
    Object.keys(fields).forEach(function(id) {
      var node = el(id);
      if (node && fields[id]) {
        node.textContent = fields[id];
      }
    });
    if (res.busy) {
      schedulePoll();
    } else {
      pollsLeft = 0;
      show(!res.error, res.error
        ? res.error
        : rcmail.gettext('donecopied', 'mail_backup').replace('$n', String(res.copied)));
    }
  });
})();
