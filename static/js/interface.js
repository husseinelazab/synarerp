/* Navigation state is ephemeral: never stores user or organization data. */
(function () {
  'use strict';
  var sidebar = document.getElementById('sidebar');
  var backdrop = document.querySelector('.nav-backdrop');
  var toggles = document.querySelectorAll('[data-nav-toggle]');
  var mobile = window.matchMedia('(max-width: 900px)');
  var previousFocus;
  function setOpen(open) {
    if (!sidebar) return;
    open = open && mobile.matches;
    sidebar.classList.toggle('open', open);
    document.body.classList.toggle('nav-is-open', open);
    if (backdrop) backdrop.hidden = !open;
    toggles.forEach(function (b) { b.setAttribute('aria-expanded', String(open)); });
    if (open) {
      previousFocus = document.activeElement;
      document.querySelectorAll('.account-menu[open]').forEach(function (d) { d.open = false; });
      var focus = sidebar.querySelector('.sidebar-dismiss');
      if (focus) { window.getComputedStyle(sidebar).visibility; focus.focus({preventScroll: true}); }
    } else if (previousFocus && document.contains(previousFocus)) {
      previousFocus.focus(); previousFocus = null;
    }
  }
  toggles.forEach(function (b) { b.addEventListener('click', function () { setOpen(!sidebar.classList.contains('open')); }); });
  document.querySelectorAll('[data-nav-close]').forEach(function (b) { b.addEventListener('click', function () { setOpen(false); }); });
  if (sidebar) sidebar.addEventListener('click', function (e) { if (e.target.closest('a')) setOpen(false); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { setOpen(false); document.querySelectorAll('.account-menu[open]').forEach(function (d) { d.open = false; d.querySelector('summary').focus(); }); }
    if (e.key !== 'Tab' || !sidebar || !sidebar.classList.contains('open')) return;
    var nodes = Array.from(sidebar.querySelectorAll('a,button,input,summary')).filter(function (n) { return n.getClientRects().length && !n.disabled; });
    var first = nodes[0], last = nodes[nodes.length - 1];
    if (!sidebar.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  if (mobile.addEventListener) mobile.addEventListener('change', function () { setOpen(false); });
  document.addEventListener('click', function (e) { document.querySelectorAll('.account-menu[open]').forEach(function (d) { if (!d.contains(e.target)) d.open = false; }); });
  document.querySelectorAll('.table-wrap').forEach(function (wrap) {
    var hint = document.createElement('div');
    hint.className = 'scroll-hint';
    hint.textContent = 'اسحب الجدول أفقيًا لعرض بقية الأعمدة';
    wrap.insertAdjacentElement('afterend', hint);
    function update() {
      var overflowing = wrap.scrollWidth > wrap.clientWidth + 2;
      hint.classList.toggle('is-needed', overflowing);
      if (overflowing) { wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', 'جدول قابل للتمرير أفقيًا'); }
      else { wrap.removeAttribute('tabindex'); wrap.removeAttribute('role'); wrap.removeAttribute('aria-label'); }
    }
    if (window.ResizeObserver) new ResizeObserver(update).observe(wrap);
    update();
  });
  document.querySelectorAll('[data-password-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var input = document.getElementById(button.getAttribute('aria-controls'));
      var reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      button.setAttribute('aria-pressed', String(reveal));
      button.setAttribute('aria-label', reveal ? 'إخفاء كلمة المرور' : 'إظهار كلمة المرور');
    });
  });
}());
