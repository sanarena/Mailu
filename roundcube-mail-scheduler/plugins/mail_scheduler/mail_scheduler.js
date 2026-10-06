/* mail_scheduler - snooze + send-later dialogs (Roundcube 1.6 / Elastic).
 * Uses only core rcmail APIs and Elastic/Bootstrap classes so any
 * Elastic-based skin (including hellomail) renders it natively. */
(function() {
  if (!window.rcmail) {
    return;
  }

  function at(hour, minute, dayOffset) {
    var d = new Date();
    d.setDate(d.getDate() + (dayOffset || 0));
    d.setHours(hour || 0, minute || 0, 0, 0);
    return d;
  }

  function nextWeekday(weekday, hour) {
    var d = new Date();
    var delta = (weekday - d.getDay() + 7) % 7;
    if (delta === 0) {
      delta = 7;
    }
    d.setDate(d.getDate() + delta);
    d.setHours(hour, 0, 0, 0);
    return d;
  }

  function fmt(d) {
    return d.toLocaleString([], { weekday: 'short', month: 'short',
      day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  function presets(kind) {
    var now = new Date();
    if (kind === 'snooze') {
      var tonight = at(21, 0, 0);
      var list = [];
      if (tonight.getTime() > now.getTime() + 5 * 60000) {
        list.push({ label: rcmail.gettext('tonight', 'mail_scheduler'), date: tonight });
      }
      list.push({ label: rcmail.gettext('tomorrowmorning', 'mail_scheduler'),
        date: at(8, 0, 1) });
      list.push({ label: rcmail.gettext('nextmonday', 'mail_scheduler'),
        date: nextWeekday(1, 8) });
      return list;
    }
    var tomorrow8 = at(8, 0, 1);
    if (tomorrow8.getTime() < now.getTime() + 3600000) {
      tomorrow8 = new Date(now.getTime() + 3600000);
    }
    return [
      { label: rcmail.gettext('tomorrowmorning', 'mail_scheduler'), date: tomorrow8 },
      { label: rcmail.gettext('tomorrowafternoon', 'mail_scheduler'), date: at(14, 0, 1) },
      { label: rcmail.gettext('nextmonday', 'mail_scheduler'), date: nextWeekday(1, 8) }
    ];
  }

  function dialogHtml(kind, list) {
    var h = '<div class="scheduler-dialog"><div class="list-group mb-3">';
    list.forEach(function(p, i) {
      h += '<button type="button" class="list-group-item list-group-item-action scheduler-preset" data-i="' + i + '">'
        + '<span>' + p.label + '</span>'
        + '<span class="text-muted float-right">' + fmt(p.date) + '</span></button>';
    });
    h += '</div><label>' + rcmail.gettext('picktime', 'mail_scheduler') + '</label>'
      + '<input type="datetime-local" id="scheduler-custom" class="form-control">'
      + '<div id="scheduler-error" class="text-danger mt-2" style="display:none"></div></div>';
    return h;
  }

  function openDialog(kind, title, onPick) {
    var list = presets(kind);
    var chosen = -1;
    var popup = rcmail.show_popup_dialog(dialogHtml(kind, list), title, [
      {
        text: rcmail.gettext('cancel', 'mail_scheduler'),
        class: 'btn btn-secondary cancel',
        click: function() { popup.dialog('close'); }
      },
      {
        text: kind === 'snooze'
          ? rcmail.gettext('snooze', 'mail_scheduler')
          : rcmail.gettext('schedule', 'mail_scheduler'),
        class: 'btn btn-primary mainaction',
        click: function() {
          var custom = document.getElementById('scheduler-custom').value;
          var when = null;
          if (custom) {
            when = new Date(custom);
          }
          else if (chosen >= 0) {
            when = list[chosen].date;
          }
          var err = document.getElementById('scheduler-error');
          if (!when || isNaN(when.getTime()) || when.getTime() < Date.now() + 60000) {
            err.textContent = rcmail.gettext('invalidtime', 'mail_scheduler');
            err.style.display = 'block';
            return;
          }
          popup.dialog('close');
          onPick(Math.floor(when.getTime() / 1000), when);
        }
      }
    ]);
    Array.prototype.forEach.call(
      popup[0].querySelectorAll('.scheduler-preset'), function(btn) {
        btn.addEventListener('click', function() {
          chosen = parseInt(btn.getAttribute('data-i'), 10);
          document.getElementById('scheduler-custom').value = '';
          Array.prototype.forEach.call(
            popup[0].querySelectorAll('.scheduler-preset'), function(b) {
              b.classList.toggle('active', b === btn);
            });
        });
      });
  }

  /* -- snooze ------------------------------------------------------ */

  function selection() {
    var uids = '';
    if (rcmail.message_list) {
      uids = rcmail.message_list.get_selection().join(',');
    }
    if (!uids && rcmail.env.uid) {
      uids = String(rcmail.env.uid);
    }
    return uids;
  }

  function toggleButtons() {
    var has = selection() !== '';
    rcmail.enable_command('plugin.scheduler.snooze', has);
    rcmail.enable_command('plugin.scheduler.unsnooze', has);
  }

  rcmail.addEventListener('init', function() {
    if (rcmail.message_list) {
      rcmail.message_list.addEventListener('select', toggleButtons);
    }
    toggleButtons();
    // custom commands start disabled: the compose button is always on
    rcmail.enable_command('plugin.scheduler.sendlater', true);
  });
  rcmail.addEventListener('listupdate', toggleButtons);

  rcmail.register_command('plugin.scheduler.snooze', function() {
    var uids = selection();
    if (!uids) {
      rcmail.display_message(rcmail.gettext('noselection', 'mail_scheduler'), 'notice');
      return;
    }
    openDialog('snooze', rcmail.gettext('snooze', 'mail_scheduler'), function(epoch) {
      rcmail.http_post('plugin.scheduler.snooze',
        { _uid: uids, _mbox: rcmail.env.mailbox, _wake: epoch });
    });
  });

  rcmail.register_command('plugin.scheduler.unsnooze', function() {
    var uids = selection();
    if (!uids) {
      rcmail.display_message(rcmail.gettext('noselection', 'mail_scheduler'), 'notice');
      return;
    }
    rcmail.http_post('plugin.scheduler.unsnooze',
      { _uid: uids, _mbox: rcmail.env.mailbox });
  });

  /* -- send later (compose) ---------------------------------------- */

  var storeOriginal = null;

  function sendAtInput(create) {
    var el = document.getElementById('scheduler-send-at');
    if (!el && create) {
      el = document.createElement('input');
      el.type = 'hidden';
      el.id = 'scheduler-send-at';
      el.name = '_send_at';
      var form = document.getElementById('form') || document.forms[0];
      if (form) {
        form.appendChild(el);
      }
    }
    return el;
  }

  function setStoreTarget(folder) {
    var sel = document.querySelector('select[name="_store_target"]');
    if (!sel) {
      return;
    }
    if (storeOriginal === null) {
      storeOriginal = sel.value;
    }
    var found = false;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === folder) {
        found = true;
        break;
      }
    }
    if (!found) {
      var opt = document.createElement('option');
      opt.value = folder;
      opt.textContent = folder;
      sel.appendChild(opt);
    }
    sel.value = folder;
  }

  function clearSchedule() {
    var el = document.getElementById('scheduler-send-at');
    if (el) {
      el.parentNode.removeChild(el);
    }
    var sel = document.querySelector('select[name="_store_target"]');
    if (sel && storeOriginal !== null) {
      sel.value = storeOriginal;
    }
    updateSendLaterButton(null);
  }

  function updateSendLaterButton(when) {
    var btn = document.querySelector('[data-command="plugin.scheduler.sendlater"] .inner')
      || document.querySelector('[data-command="plugin.scheduler.sendlater"]');
    if (!btn) {
      return;
    }
    btn.textContent = when
      ? rcmail.gettext('scheduledfor', 'mail_scheduler') + ' ' + fmt(when)
      : rcmail.gettext('sendlater', 'mail_scheduler');
  }

  rcmail.register_command('plugin.scheduler.sendlater', function() {
    var existing = sendAtInput(false);
    if (existing && existing.value) {
      // already scheduled: offer to send now instead
      var popup = rcmail.show_popup_dialog(
        '<p>' + rcmail.gettext('scheduledfor', 'mail_scheduler') + ' '
          + fmt(new Date(parseInt(existing.value, 10) * 1000)) + '</p>',
        rcmail.gettext('sendlater', 'mail_scheduler'), [
          {
            text: rcmail.gettext('sendnow', 'mail_scheduler'),
            class: 'btn btn-secondary',
            click: function() {
              clearSchedule();
              popup.dialog('close');
            }
          },
          {
            text: rcmail.gettext('keep', 'mail_scheduler'),
            class: 'btn btn-primary',
            click: function() { popup.dialog('close'); }
          }
        ]);
      return;
    }
    openDialog('sendlater', rcmail.gettext('sendlater', 'mail_scheduler'),
      function(epoch, when) {
        var el = sendAtInput(true);
        if (!el) {
          return;
        }
        el.value = String(epoch);
        setStoreTarget('Scheduled');
        updateSendLaterButton(when);
        rcmail.command('send');
      });
  });
})();
