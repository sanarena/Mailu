/* compose_from: From as a combo box (type any address or pick an identity).
 *
 * Roundcube's <select name="_from"> stays in the form, hidden, and remains
 * the value that is sent: picking an identity selects its option (and runs
 * Roundcube's change_identity for signatures); a typed address is added as
 * an extra option whose value is the address text, which Roundcube's send
 * code treats as a custom From. Text is written with textContent only. */
(function() {
  if (typeof rcmail === 'undefined') {
    return;
  }

  var ADDR_RE = /[^\s<>"@,;]+@[^\s<>"@,;]+\.[^\s<>"@,;]+/;

  function label(name) {
    return rcmail.gettext(name, 'compose_from');
  }

  function build(select) {
    var group = select.parentNode;
    var wrap = document.createElement('div');
    wrap.className = 'compose-from-combo';
    var input = document.createElement('input');
    input.type = 'text';
    input.id = '_from_text';
    input.className = 'form-control';
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', 'compose-from-list');
    input.setAttribute('aria-label', label('fromlabel'));
    input.placeholder = label('placeholder');
    if (select.tabIndex) {
      input.tabIndex = select.tabIndex;
    }
    var toggle = document.createElement('a');
    toggle.href = '#';
    toggle.className = 'compose-from-toggle';
    toggle.setAttribute('role', 'button');
    toggle.setAttribute('tabindex', '-1');
    toggle.title = label('showidentities');
    var list = document.createElement('ul');
    list.id = 'compose-from-list';
    list.className = 'compose-from-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;
    var hint = document.createElement('div');
    hint.className = 'compose-from-hint';
    hint.setAttribute('aria-live', 'polite');

    wrap.appendChild(input);
    wrap.appendChild(toggle);
    wrap.appendChild(list);
    group.insertBefore(wrap, select);
    select.style.display = 'none';
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;
    // hint below the whole input group (the column), not inside the flex row
    group.parentNode.insertBefore(hint, group.nextSibling);

    var identities = [];
    Array.prototype.forEach.call(select.options, function(opt) {
      if (!opt.hasAttribute('data-custom')) {
        var email = (rcmail.env.identities && rcmail.env.identities[opt.value]
          && rcmail.env.identities[opt.value].email) || '';
        identities.push({id: opt.value, text: opt.text, email: email.toLowerCase()});
      }
    });

    var active = -1;
    var checkTimer = null;
    var lastChecked = null;

    function customOption() {
      return select.querySelector('option[data-custom]');
    }

    function setHint(state, text) {
      hint.className = 'compose-from-hint' + (state ? ' ' + state : '');
      hint.textContent = text || '';
      input.classList.toggle('is-invalid', state === 'bad');
    }

    function matchIdentity(text) {
      var t = text.trim().toLowerCase();
      var m = t.match(ADDR_RE);
      var addr = m ? m[0].toLowerCase() : '';
      for (var i = 0; i < identities.length; i++) {
        var ident = identities[i];
        if (ident.text.toLowerCase() === t) {
          return ident;
        }
      }
      // a bare address equal to an identity's address selects that identity
      if (addr && t === addr) {
        for (var j = 0; j < identities.length; j++) {
          if (identities[j].email === addr) {
            return identities[j];
          }
        }
      }
      return null;
    }

    function pickIdentity(ident) {
      var custom = customOption();
      if (custom) {
        custom.remove();
      }
      var changed = select.value !== ident.id;
      select.value = ident.id;
      input.value = ident.text;
      lastChecked = null;
      setHint('', '');
      if (changed) {
        rcmail.change_identity(select);
      }
    }

    function useCustom(text) {
      var custom = customOption();
      if (!custom) {
        custom = document.createElement('option');
        custom.setAttribute('data-custom', '1');
        select.appendChild(custom);
      }
      custom.value = text;
      custom.textContent = text;
      select.value = text;
      // no change_identity: the current signature/Reply-To stay as they are
      check(text);
    }

    function check(text) {
      clearTimeout(checkTimer);
      if (text === lastChecked) {
        return;
      }
      lastChecked = text;
      setHint('pending', label('checking'));
      checkTimer = setTimeout(function() {
        rcmail.http_post('plugin.compose_from.check', {_address: text}, false);
      }, 150);
    }

    function commit() {
      var text = input.value.trim();
      if (!text) {
        // empty: fall back to the selected identity
        var cur = select.options[select.selectedIndex];
        input.value = cur ? cur.text : '';
        setHint('', '');
        return;
      }
      var ident = matchIdentity(text);
      if (ident) {
        pickIdentity(ident);
      }
      else if (!ADDR_RE.test(text)) {
        setHint('bad', label('invalid'));
      }
      else {
        useCustom(text);
      }
    }

    function render(filter) {
      while (list.firstChild) {
        list.removeChild(list.firstChild);
      }
      var f = (filter || '').trim().toLowerCase();
      // typed text: only matching identities (none = the user is typing a
      // custom address, so no list covering the fields below); empty text
      // or the caret/arrow key: all identities
      var shown = identities.filter(function(ident) {
        return !f || ident.text.toLowerCase().indexOf(f) !== -1;
      });
      shown.forEach(function(ident, i) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.id = 'compose-from-opt-' + i;
        li.textContent = ident.text;
        li.dataset.id = ident.id;
        if (ident.id === select.value) {
          li.setAttribute('aria-selected', 'true');
        }
        li.addEventListener('mousedown', function(e) {
          e.preventDefault();  // keep focus in the input
          pickIdentity(ident);
          close();
        });
        list.appendChild(li);
      });
      active = -1;
      return shown.length;
    }

    function open(filter) {
      if (!render(filter)) {
        close();
        return;
      }
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function move(delta) {
      var items = list.querySelectorAll('li');
      if (!items.length) {
        return;
      }
      if (active >= 0) {
        items[active].classList.remove('active');
      }
      active = (active + delta + items.length) % items.length;
      items[active].classList.add('active');
      input.setAttribute('aria-activedescendant', items[active].id);
      items[active].scrollIntoView({block: 'nearest'});
    }

    input.addEventListener('input', function() {
      lastChecked = null;
      setHint('', '');
      var text = input.value.trim().toLowerCase();
      var exact = identities.some(function(ident) {
        return ident.text.toLowerCase() === text;
      });
      if (exact) {
        close();
      }
      else {
        open(input.value);
      }
    });
    input.addEventListener('focus', function() {
      input.select();
    });
    input.addEventListener('keydown', function(e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (list.hidden) {
          open('');
        }
        move(1);
      }
      else if (e.key === 'ArrowUp') {
        e.preventDefault();
        move(-1);
      }
      else if (e.key === 'Enter') {
        e.preventDefault();  // never submit the compose form from here
        var items = list.querySelectorAll('li');
        if (!list.hidden && active >= 0 && items[active]) {
          var id = items[active].dataset.id;
          identities.forEach(function(ident) {
            if (ident.id === id) {
              pickIdentity(ident);
            }
          });
        }
        else {
          commit();
        }
        close();
      }
      else if (e.key === 'Escape') {
        if (!list.hidden) {
          e.preventDefault();
          e.stopPropagation();
          close();
        }
      }
    });
    input.addEventListener('blur', function() {
      close();
      commit();
    });
    toggle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      if (list.hidden) {
        input.focus();
        open('');
      }
      else {
        close();
      }
    });
    toggle.addEventListener('click', function(e) {
      e.preventDefault();
    });

    rcmail.addEventListener('plugin.compose_from_checked', function(res) {
      if (!res || res.address !== lastChecked) {
        return;  // a newer address is being checked
      }
      if (res.state === 'ok') {
        setHint('ok', label('allowed'));
      }
      else if (res.state === 'denied') {
        setHint('bad', label('denied'));
      }
      else if (res.state === 'invalid') {
        setHint('bad', label('invalid'));
      }
      else {
        setHint('', '');
      }
    });

    // always send/save what is typed, even without leaving the field
    ['beforesend', 'beforesavedraft'].forEach(function(ev) {
      rcmail.addEventListener(ev, function() {
        if (document.activeElement === input) {
          commit();
        }
      });
    });

    // initial value: a custom From from the server (draft / reply), else
    // the identity Roundcube selected
    if (rcmail.env.compose_from_custom) {
      input.value = rcmail.env.compose_from_custom;
      useCustom(rcmail.env.compose_from_custom);
    }
    else {
      var cur = select.options[select.selectedIndex];
      input.value = cur ? cur.text : '';
    }
  }

  rcmail.addEventListener('init', function() {
    if (rcmail.env.action !== 'compose') {
      return;
    }
    var select = document.getElementById('_from');
    // without identities Roundcube already renders a text input
    if (select && select.tagName === 'SELECT' && !document.getElementById('_from_text')) {
      build(select);
    }
  });
})();
