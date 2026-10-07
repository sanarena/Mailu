/* mail_tracking: compose toggle (mail task) + tracking task app.
 * All server strings are rendered via textContent (events carry
 * untrusted IP/UA/location data). No innerHTML for data. */
(function() {
  if (typeof rcmail === 'undefined') {
    return;
  }

  /* -- compose toggle ---------------------------------------------- */

  function trackInput(create) {
    var form = document.getElementById('form') || document.forms[0];
    var el = document.getElementById('tracking-track');
    if (!el && create && form) {
      el = document.createElement('input');
      el.type = 'hidden';
      el.id = 'tracking-track';
      el.name = '_track';
      el.value = '1';
      form.appendChild(el);
    }
    return el;
  }

  function injectTrackToggle() {
    if (rcmail.task !== 'mail' || rcmail.env.action !== 'compose') {
      return;
    }
    if (document.getElementById('tracking-toggle')) {
      return;
    }
    // Anchor after the "Save sent message in" row; fall back to the
    // composition options region, then to the form itself.
    var anchor = document.getElementById('compose-store-target');
    var row = anchor && anchor.closest ? anchor.closest('.form-group, .row') : null;
    var box = (row && row.parentNode) || document.querySelector('#composeoptions, #compose-options')
      || document.getElementById('form') || document.body;
    var wrap = document.createElement('div');
    wrap.className = 'form-group row tracking-toggle-row';
    var lab = document.createElement('label');
    lab.className = 'col-form-label col-6';
    lab.setAttribute('for', 'tracking-toggle');
    lab.textContent = rcmail.gettext('tracklabel', 'mail_tracking');
    var cell = document.createElement('div');
    cell.className = 'col-6';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'tracking-toggle';
    cb.className = 'form-check-input';
    cb.checked = true;
    cb.addEventListener('change', function() {
      var el = trackInput(true);
      if (el) {
        el.value = cb.checked ? '1' : '0';
      }
    });
    cell.appendChild(cb);
    wrap.appendChild(lab);
    wrap.appendChild(cell);
    if (row && row.parentNode === box) {
      box.insertBefore(wrap, row.nextSibling);
    }
    else {
      box.appendChild(wrap);
    }
    trackInput(true);
  }

  /* -- tracking task app -------------------------------------------- */

  function el(id) {
    return document.getElementById(id);
  }

  function txt(tag, s, cls) {
    var e = document.createElement(tag);
    if (cls) {
      e.className = cls;
    }
    e.textContent = s === null || s === undefined ? '' : String(s);
    return e;
  }

  function fmtTime(epoch) {
    if (!epoch) {
      return '';
    }
    return new Date(epoch * 1000).toLocaleString();
  }

  function fail(msg) {
    var box = el('tracking-error');
    if (box) {
      box.textContent = msg || rcmail.gettext('loaderror', 'mail_tracking');
      box.classList.remove('hidden');
    }
  }

  function clearFail() {
    var box = el('tracking-error');
    if (box) {
      box.classList.add('hidden');
    }
  }

  var state = { offset: 0, limit: 50, loading: false, byId: {} };

  function loadStats() {
    rcmail.http_post('stats', {});
  }

  function loadMessages(reset) {
    if (state.loading) {
      return;
    }
    if (reset) {
      state.offset = 0;
      state.byId = {};
      var tb = document.querySelector('#tracking-messages tbody');
      if (tb) {
        tb.innerHTML = '';
      }
    }
    state.loading = true;
    rcmail.http_post('messages', { limit: state.limit, offset: state.offset });
  }

  function loadDetail(id) {
    rcmail.http_post('detail', { id: id });
  }

  function loadOptouts() {
    rcmail.http_post('optouts', {});
  }

  function renderStats(res) {
    if (!res || res.error) {
      fail();
      return;
    }
    var box = el('tracking-stats');
    if (!box) {
      return;
    }
    box.innerHTML = '';
    var pct = Math.round((res.open_rate || 0) * 100) + '%';
    var cards = [
      [res.sent || 0, rcmail.gettext('statssent', 'mail_tracking')],
      [res.opened_messages || 0, rcmail.gettext('statsopened', 'mail_tracking')],
      [res.opens || 0, rcmail.gettext('statsopens', 'mail_tracking')],
      [res.clicks || 0, rcmail.gettext('statsclicks', 'mail_tracking')],
      [pct, rcmail.gettext('statsopenrate', 'mail_tracking')]
    ];
    cards.forEach(function(c) {
      var d = document.createElement('div');
      d.className = 'tracking-stat';
      d.appendChild(txt('div', c[0], 'n'));
      d.appendChild(txt('div', c[1], 'l'));
      box.appendChild(d);
    });
  }

  function renderMessages(res) {
    state.loading = false;
    if (!res || res.error) {
      fail();
      return;
    }
    var tb = document.querySelector('#tracking-messages tbody');
    if (!tb) {
      return;
    }
    (res.messages || []).forEach(function(m) {
      state.byId[m.id] = m;
      var tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.appendChild(txt('td', (m.recipients || []).join(', ')));
      tr.appendChild(txt('td', m.subject || ''));
      tr.appendChild(txt('td', fmtTime(m.sent_at)));
      tr.appendChild(txt('td', m.opens || 0));
      tr.appendChild(txt('td', m.clicks || 0));
      tr.addEventListener('click', function() {
        loadDetail(m.id);
      });
      tb.appendChild(tr);
    });
    state.offset += (res.messages || []).length;
    var more = el('tracking-more');
    if (more) {
      more.classList.toggle('hidden', !res.has_more);
    }
    var empty = el('tracking-empty');
    if (empty) {
      empty.classList.toggle('hidden', state.offset > 0);
    }
  }

  function renderDetail(res) {
    var box = el('tracking-detail');
    if (!box) {
      return;
    }
    if (!res || res.error || !res.id) {
      fail();
      return;
    }
    clearFail();
    var m = res;
    var evs = res.events || [];
    var links = {};
    (res.links || []).forEach(function(l) {
      links[l.idx] = l.url;
    });
    box.innerHTML = '';
    box.classList.remove('hidden');

    var back = document.createElement('button');
    back.className = 'btn btn-secondary tracking-back';
    back.textContent = rcmail.gettext('backtolist', 'mail_tracking');
    back.addEventListener('click', function() {
      box.classList.add('hidden');
      box.innerHTML = '';
    });
    box.appendChild(back);

    var dl = document.createElement('dl');
    dl.className = 'row';
    dl.appendChild(txt('dt', rcmail.gettext('colsubject', 'mail_tracking'), 'col-3'));
    dl.appendChild(txt('dd', m.subject || '', 'col-9'));
    dl.appendChild(txt('dt', rcmail.gettext('recipients', 'mail_tracking'), 'col-3'));
    dl.appendChild(txt('dd', (m.recipients || []).join(', '), 'col-9'));
    dl.appendChild(txt('dt', rcmail.gettext('sentat', 'mail_tracking'), 'col-3'));
    dl.appendChild(txt('dd', fmtTime(m.sent_at), 'col-9'));
    box.appendChild(dl);

    var tgl = document.createElement('div');
    tgl.className = 'form-check mb-2';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'tracking-detail-toggle';
    cb.className = 'form-check-input';
    cb.checked = !!m.tracking;
    cb.addEventListener('change', function() {
      rcmail.http_post('set_tracking', { id: m.id, tracking: cb.checked ? '1' : '0' });
    });
    var lb = document.createElement('label');
    lb.className = 'form-check-label';
    lb.setAttribute('for', 'tracking-detail-toggle');
    lb.textContent = rcmail.gettext('trackingon', 'mail_tracking');
    tgl.appendChild(cb);
    tgl.appendChild(lb);
    box.appendChild(tgl);

    box.appendChild(txt('h3', rcmail.gettext('events', 'mail_tracking')));
    if (!evs.length) {
      box.appendChild(txt('p', rcmail.gettext('noevents', 'mail_tracking')));
      box.scrollIntoView();
      return;
    }
    var table = document.createElement('table');
    table.id = 'tracking-events';
    table.className = 'table table-striped';
    var body = document.createElement('tbody');
    evs.forEach(function(e) {
      var tr = document.createElement('tr');
      var kind = e.kind === 'click'
        ? rcmail.gettext('eventclick', 'mail_tracking')
        : rcmail.gettext('eventopen', 'mail_tracking');
      tr.appendChild(txt('td', kind));
      tr.appendChild(txt('td', fmtTime(e.at)));
      var where = [e.geo_city, e.geo_country].filter(Boolean).join(', ') || (e.ip || '');
      tr.appendChild(txt('td', where));
      var agent = e.client || '';
      if (e.device) {
        agent += (agent ? ' · ' : '') + e.device;
      }
      tr.appendChild(txt('td', agent));
      if (e.kind === 'click' && links[e.link_idx]) {
        var td = document.createElement('td');
        var a = document.createElement('a');
        var url = String(links[e.link_idx]);
        if (/^https?:\/\//.test(url)) {
          a.href = url;
          a.target = '_blank';
          a.rel = 'noopener';
        }
        a.textContent = url.length > 60 ? url.slice(0, 60) + '…' : url;
        td.appendChild(a);
        tr.appendChild(td);
      }
      else {
        tr.appendChild(txt('td', ''));
      }
      body.appendChild(tr);
    });
    table.appendChild(body);
    box.appendChild(table);
    box.scrollIntoView();
  }

  function renderOptouts(res) {
    if (!res || res.error) {
      fail();
      return;
    }
    var sender = el('tracking-optsender');
    if (sender) {
      sender.checked = !!res.sender_opted_out;
    }
    var ul = el('tracking-optlist');
    if (ul) {
      ul.innerHTML = '';
      (res.recipients || []).forEach(function(addr) {
        var li = document.createElement('li');
        li.className = 'mb-1';
        var span = txt('span', addr + ' ');
        var btn = document.createElement('button');
        btn.className = 'btn btn-sm btn-secondary';
        btn.textContent = rcmail.gettext('optremove', 'mail_tracking');
        btn.addEventListener('click', function() {
          rcmail.http_post('optouts', {
            _m: 'POST', scope: 'recipient', address: addr, opted_out: '0'
          });
        });
        li.appendChild(span);
        li.appendChild(btn);
        ul.appendChild(li);
      });
    }
  }

  function bindTask() {
    if (rcmail.task !== 'tracking') {
      return;
    }
    rcmail.addEventListener('plugin.tracking.stats', renderStats);
    rcmail.addEventListener('plugin.tracking.messages', renderMessages);
    rcmail.addEventListener('plugin.tracking.detail', renderDetail);
    rcmail.addEventListener('plugin.tracking.set', function(res) {
      if (!res || res.error) {
        fail(rcmail.gettext('saveerror', 'mail_tracking'));
        return;
      }
      loadDetail(res.id);
    });
    rcmail.addEventListener('plugin.tracking.optouts', function(res) {
      if (!res || res.error) {
        fail(rcmail.gettext('saveerror', 'mail_tracking'));
        return;
      }
      // POST returns {ok}; reload to show fresh state
      if (res.ok && !('sender_opted_out' in res)) {
        loadOptouts();
        return;
      }
      renderOptouts(res);
    });
    var more = el('tracking-more');
    if (more) {
      more.addEventListener('click', function() {
        loadMessages(false);
      });
    }
    var sender = el('tracking-optsender');
    if (sender) {
      sender.addEventListener('change', function() {
        rcmail.http_post('optouts', {
          _m: 'POST', scope: 'sender', opted_out: sender.checked ? '1' : '0'
        });
      });
    }
    var addBtn = el('tracking-optadd-btn');
    if (addBtn) {
      addBtn.addEventListener('click', function() {
        var inp = el('tracking-optadd');
        var addr = inp ? inp.value.trim() : '';
        if (addr.indexOf('@') < 0) {
          return;
        }
        inp.value = '';
        rcmail.http_post('optouts', {
          _m: 'POST', scope: 'recipient', address: addr, opted_out: '1'
        });
      });
    }
    loadStats();
    loadMessages(true);
    loadOptouts();
    if (rcmail.env.tracking_open_id) {
      loadDetail(rcmail.env.tracking_open_id);
    }
  }

  rcmail.addEventListener('init', function() {
    injectTrackToggle();
    bindTask();
  });
})();
