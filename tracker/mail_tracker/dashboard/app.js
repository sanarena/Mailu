(function () {
  'use strict';

  var state = { me: null, page: 0, limit: 25, total: 0, all: false, detail: null };
  var $ = function (id) { return document.getElementById(id); };

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function api(method, path, data) {
    return fetch(path, {
      method: method,
      credentials: 'same-origin',
      headers: data === undefined ? {} : { 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data)
    }).then(function (resp) {
      if (resp.status === 401) { showLogin(); throw new Error('login'); }
      return resp.json().then(function (body) {
        if (!resp.ok) { throw new Error(body.error || 'request failed'); }
        return body;
      });
    });
  }

  function relTime(epoch) {
    if (!epoch) { return '–'; }
    var diff = Math.floor(Date.now() / 1000 - epoch);
    if (diff < 60) { return 'just now'; }
    if (diff < 3600) { return Math.floor(diff / 60) + 'm ago'; }
    if (diff < 86400) { return Math.floor(diff / 3600) + 'h ago'; }
    var days = Math.floor(diff / 86400);
    if (days < 30) { return days + 'd ago'; }
    return new Date(epoch * 1000).toLocaleDateString();
  }

  function absTime(epoch) {
    return epoch ? new Date(epoch * 1000).toLocaleString() : '';
  }

  function where(ev) {
    var bits = [];
    if (ev.geo_city) { bits.push(ev.geo_city); }
    if (ev.geo_country) { bits.push(ev.geo_country); }
    var loc = bits.join(', ');
    var rest = [];
    if (ev.client && ev.client !== 'Unknown') { rest.push(ev.client); }
    if (ev.device && ev.device !== 'Unknown') { rest.push(ev.device); }
    if (ev.ip) { rest.push(ev.ip); }
    return (loc ? loc + ' — ' : '') + (rest.join(' · ') || 'unknown reader');
  }

  /* views */
  function showLogin() {
    $('login-view').classList.remove('hidden');
    $('app-view').classList.add('hidden');
  }

  function showApp() {
    $('login-view').classList.add('hidden');
    $('app-view').classList.remove('hidden');
    $('user-email').textContent = state.me.user;
    $('user-initial').textContent = state.me.user.charAt(0).toUpperCase();
    if (state.me.admin) { $('all-wrap').classList.remove('hidden'); $('admin-card').classList.remove('hidden'); }
    switchView('sent');
  }

  function switchView(name) {
    ['sent', 'detail', 'compose', 'settings'].forEach(function (v) {
      $('view-' + v).classList.toggle('hidden', v !== name);
    });
    var tabs = document.querySelectorAll('.topbar nav button');
    tabs.forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-view') === name ||
        (name === 'detail' && btn.getAttribute('data-view') === 'sent'));
    });
    if (name === 'sent') { loadSent(); }
    if (name === 'settings') { loadSettings(); }
  }

  /* sent list */
  function loadStats() {
    var q = state.all ? '?all=1' : '';
    api('GET', '/api/stats' + q).then(function (s) {
      $('stat-sent').textContent = s.sent;
      $('stat-opened').textContent = s.opened_messages + ' / ' + s.sent;
      $('stat-rate').textContent = Math.round(s.open_rate * 100) + '%';
      $('stat-clicks').textContent = s.clicks;
    }).catch(function () {});
  }

  function loadSent() {
    var q = '?limit=' + state.limit + '&offset=' + (state.page * state.limit) +
      (state.all ? '&all=1' : '');
    loadStats();
    api('GET', '/api/messages' + q).then(function (data) {
      state.total = data.total;
      var rows = data.messages.map(function (m) {
        var dot = !m.tracking ? 'off' : (m.opens > 0 ? 'opened' : 'unopened');
        var to = m.recipients.length > 1
          ? m.recipients[0] + ' +' + (m.recipients.length - 1)
          : (m.recipients[0] || '–');
        return '<tr data-id="' + m.id + '">' +
          '<td><span class="dot ' + dot + '"></span></td>' +
          '<td>' + esc(to) + '</td>' +
          '<td>' + esc(m.subject || '(no subject)') + '</td>' +
          '<td class="hide-sm muted" title="' + esc(absTime(m.sent_at)) + '">' +
          esc(relTime(m.sent_at)) + '</td>' +
          '<td class="num">' + m.opens + '</td>' +
          '<td class="num hide-sm">' + m.clicks + '</td>' +
          '<td class="hide-sm muted">' + esc(relTime(m.last_open)) + '</td>' +
          '</tr>';
      }).join('');
      $('sent-rows').innerHTML = rows ||
        '<tr><td colspan="7" class="muted">No tracked mail yet.</td></tr>';
      var pages = Math.max(1, Math.ceil(data.total / state.limit));
      $('page-info').textContent = 'Page ' + (state.page + 1) + ' of ' + pages;
      $('prev-btn').disabled = state.page === 0;
      $('next-btn').disabled = (state.page + 1) * state.limit >= data.total;
    }).catch(function (err) {
      if (err.message === 'login') { return; }
      var box = $('sent-error');
      box.textContent = err.message;
      box.classList.remove('hidden');
    });
  }

  /* detail */
  function openDetail(id) {
    api('GET', '/api/messages/' + id).then(function (m) {
      state.detail = m;
      $('detail-subject').textContent = m.subject || '(no subject)';
      $('detail-meta').textContent = 'from ' + m.sender + ' · sent ' +
        absTime(m.sent_at);
      var btn = $('tracking-btn');
      btn.textContent = m.tracking ? 'Disable tracking' : 'Re-enable tracking';
      btn.className = m.tracking ? 'btn danger small' : 'btn small';
      $('detail-recipients').innerHTML = '<p class="sub">Recipients</p><ul class="optlist">' +
        m.recipients.map(function (r) {
          return '<li><span style="flex:1">' + esc(r) + '</span>' +
            '<button class="btn ghost small" data-optout="' + esc(r) + '">Don\'t track</button></li>';
        }).join('') + '</ul>';
      $('detail-events').innerHTML = m.events.map(function (ev) {
        return '<li><span class="kind ' + esc(ev.kind) + '">' + esc(ev.kind) +
          '</span><time title="' + esc(absTime(ev.at)) + '">' +
          esc(absTime(ev.at)) + '</time><span>' + esc(where(ev)) + '</span></li>';
      }).join('');
      $('detail-empty').classList.toggle('hidden', m.events.length > 0);
      switchView('detail');
    }).catch(function () {});
  }

  /* settings */
  function loadSettings() {
    api('GET', '/api/optouts').then(function (data) {
      $('sender-optout').checked = !!data.sender_opted_out;
      $('optout-list').innerHTML = data.recipients.map(function (r) {
        return '<li><span style="flex:1">' + esc(r) + '</span>' +
          '<button class="btn ghost small" data-unopt="' + esc(r) + '">Remove</button></li>';
      }).join('') || '<li class="muted">Nobody opted out.</li>';
      if (data.all_sender_optouts) {
        $('admin-optouts').innerHTML = data.all_sender_optouts.map(function (r) {
          return '<li><span style="flex:1">' + esc(r) + '</span>' +
            '<button class="btn ghost small" data-unsender="' + esc(r) + '">Remove</button></li>';
        }).join('') || '<li class="muted">Nobody opted out.</li>';
      }
    }).catch(function () {});
  }

  /* compose */
  var composeTimer = null;
  function refreshComposeDefault() {
    var to = $('compose-to').value.split(',').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    if (!to.length) { return; }
    api('POST', '/api/optouts/check', { recipients: to }).then(function (res) {
      $('compose-track').checked = !!res.track;
      $('compose-track-note').textContent =
        res.reason || res.warning || 'Record opens and link clicks.';
    }).catch(function () {});
  }

  /* wiring */
  document.querySelectorAll('.topbar nav button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      switchView(btn.getAttribute('data-view'));
    });
  });

  $('login-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var box = $('login-error');
    box.classList.add('hidden');
    api('POST', '/api/login', {
      user: $('login-user').value, password: $('login-pass').value
    }).then(function (me) {
      $('login-pass').value = '';
      state.me = me;
      state.page = 0;
      showApp();
    }).catch(function (err) {
      if (err.message === 'login') { return; }
      box.textContent = err.message;
      box.classList.remove('hidden');
    });
  });

  $('logout-btn').addEventListener('click', function () {
    api('POST', '/api/logout').finally(showLogin);
  });

  $('all-toggle').addEventListener('change', function (ev) {
    state.all = ev.target.checked;
    state.page = 0;
    loadSent();
  });
  $('prev-btn').addEventListener('click', function () {
    if (state.page > 0) { state.page -= 1; loadSent(); }
  });
  $('next-btn').addEventListener('click', function () {
    state.page += 1; loadSent();
  });
  $('sent-rows').addEventListener('click', function (ev) {
    var row = ev.target.closest('tr[data-id]');
    if (row) { openDetail(row.getAttribute('data-id')); }
  });
  $('back-btn').addEventListener('click', function () { switchView('sent'); });

  $('tracking-btn').addEventListener('click', function () {
    if (!state.detail) { return; }
    api('POST', '/api/messages/' + state.detail.id + '/tracking',
      { enabled: !state.detail.tracking }).then(function () {
      openDetail(state.detail.id);
      loadSent();
    }).catch(function () {});
  });

  document.addEventListener('click', function (ev) {
    var t = ev.target;
    if (t.hasAttribute('data-optout')) {
      api('POST', '/api/optouts', {
        scope: 'recipient', address: t.getAttribute('data-optout'), opted_out: true
      }).then(function () { openDetail(state.detail.id); }).catch(function () {});
    }
    if (t.hasAttribute('data-unopt')) {
      api('POST', '/api/optouts', {
        scope: 'recipient', address: t.getAttribute('data-unopt'), opted_out: false
      }).then(loadSettings).catch(function () {});
    }
    if (t.hasAttribute('data-unsender')) {
      api('POST', '/api/optouts', {
        scope: 'sender', address: t.getAttribute('data-unsender'), opted_out: false
      }).then(loadSettings).catch(function () {});
    }
  });

  $('sender-optout').addEventListener('change', function (ev) {
    api('POST', '/api/optouts',
      { scope: 'sender', opted_out: ev.target.checked }).catch(function () {});
  });
  $('optout-add').addEventListener('click', function () {
    var input = $('optout-input');
    if (!input.value) { return; }
    api('POST', '/api/optouts',
      { scope: 'recipient', address: input.value, opted_out: true }
    ).then(function () { input.value = ''; loadSettings(); }).catch(function () {});
  });

  $('compose-to').addEventListener('input', function () {
    clearTimeout(composeTimer);
    composeTimer = setTimeout(refreshComposeDefault, 500);
  });
  $('compose-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var errBox = $('compose-error');
    var okBox = $('compose-ok');
    errBox.classList.add('hidden');
    okBox.classList.add('hidden');
    var to = $('compose-to').value.split(',').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    api('POST', '/api/compose', {
      to: to,
      subject: $('compose-subject').value,
      body: $('compose-body').value,
      track: $('compose-track').checked,
      password: $('compose-pass').value
    }).then(function () {
      $('compose-pass').value = '';
      okBox.textContent = 'Sent.';
      okBox.classList.remove('hidden');
    }).catch(function (err) {
      if (err.message === 'login') { return; }
      errBox.textContent = err.message;
      errBox.classList.remove('hidden');
    });
  });

  /* boot */
  api('GET', '/api/me').then(function (me) {
    state.me = me;
    showApp();
  }).catch(function () { showLogin(); });
})();
