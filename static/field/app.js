(function () {
  'use strict';
  const $ = id => document.getElementById(id), KEY = 'synar-field-v1';
  let tasks = [], queue = [], profile = '', company = '', busy = false;
  const kind = row => row.kind || 'beneficiary-follow-up';
  const rowKey = row => kind(row) + ':' + row.id + (kind(row) === 'quran-progress' ? ':' + row.day : '');
  const csrf = () => { const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; };
  function persist() { if ($('offline').checked && profile) localStorage.setItem(KEY, JSON.stringify({ profile, company, tasks, queue })); }
  function status(text) { $('status').textContent = text; }
  function line(parent, text, tag = 'p') { const e = document.createElement(tag); e.textContent = text; parent.appendChild(e); return e; }
  function render() {
    $('company').textContent = company; $('tasks').replaceChildren(); $('outbox').replaceChildren();
    if (!tasks.length) line($('tasks'), 'لا توجد مهام محملة.');
    queue.forEach(entry => {
      const div = document.createElement('div'); div.className = 'task'; line(div, entry.title, 'strong'); line(div, entry.note);
      if (entry.error) {
        line(div, entry.error).className = 'error';
        const link = line(div, 'فتح السجل الحالي للمراجعة', 'a');
        link.href = '/app/' + (kind(entry) === 'quran-progress' ? 'quran-student' : kind(entry)) + '/' + entry.id + '/'; link.target = '_blank'; link.rel = 'noopener';
        const retry = line(div, 'إعادة تجهيز البيانات بعد مراجعة التعارض', 'button');
        retry.onclick = async () => {
          await refresh(); const current = tasks.find(t => rowKey(t) === rowKey(entry));
          if (!current) return status('السجل أو تاريخ الرصد لم يعد ضمن التكليف الحالي. احتفظ بالملاحظة وراجع المسؤول.');
          if (!confirm('هل راجعت النسخة الحالية وتريد إرسال هذه البيانات إليها؟')) return;
          entry.version = current.version; entry.token = crypto.randomUUID(); delete entry.error; persist(); render(); if (navigator.onLine) sync();
        };
      }
      $('outbox').appendChild(div);
    });
    if (!queue.length) line($('outbox'), 'لا توجد ملاحظات بانتظار المزامنة.');
    tasks.forEach(task => {
      const quran = kind(task) === 'quran-progress', d = document.createElement('div'); d.className = 'task'; d.dataset.taskId = task.id; d.dataset.kind = kind(task);
      line(d, task.title, 'h3'); line(d, task.file + ' · ' + (task.due || 'بلا موعد'));
      const note = document.createElement('textarea'); note.maxLength = 2000; note.placeholder = quran ? 'ملاحظة الرصد (اختياري)' : 'نتيجة الزيارة أو المتابعة'; d.appendChild(note);
      const label = document.createElement('label'), done = document.createElement('input'); done.type = 'checkbox'; done.checked = quran ? task.attended : false;
      label.append(done, document.createTextNode(quran ? ' حاضر في الحلقة' : ' تم إنجاز المهمة')); d.appendChild(label);
      let pages, score;
      if (quran) {
        const pl = line(d, 'صفحات الحفظ المنجزة', 'label'); pages = document.createElement('input'); pages.type = 'number'; pages.min = 0; pages.max = 99999; pages.step = '0.01'; pages.value = task.pages; pl.appendChild(pages);
        const sl = line(d, 'التقييم من 100 (اتركه فارغًا عند عدم التقييم)', 'label'); score = document.createElement('input'); score.type = 'number'; score.min = 0; score.max = 100; score.value = task.score === null ? '' : task.score; sl.appendChild(score);
      }
      const button = line(d, 'حفظ الملاحظة للمزامنة', 'button'); button.disabled = queue.some(q => rowKey(q) === rowKey(task));
      button.onclick = () => {
        if (!quran && !note.value.trim()) return status('اكتب نتيجة المتابعة أولًا.');
        if (quran && (!pages.reportValidity() || !score.reportValidity())) return;
        if (!navigator.onLine && !$('offline').checked) return status('فعّل الحفظ على جهازك الشخصي قبل العمل دون اتصال.');
        const entry = { kind: kind(task), id: task.id, version: task.version, note: note.value.trim() || 'رصد حضور وحفظ ميداني', completed: quran ? false : done.checked, token: crypto.randomUUID(), title: task.title };
        if (quran) Object.assign(entry, { day: task.day, attended: done.checked, pages: pages.value || '0', score: score.value === '' ? null : Number(score.value) });
        queue.push(entry); persist(); render(); status('حُفظ الرصد في قائمة المزامنة.'); if (navigator.onLine) sync();
      };
      $('tasks').appendChild(d);
    });
  }
  async function refresh() {
    try {
      const r = await fetch('/work/field/data/', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok || !r.headers.get('content-type').includes('application/json')) throw Error('يلزم تسجيل الدخول والصلاحية لتحديث المهام.');
      const data = await r.json();
      if (profile && profile !== data.profile) {
        if (queue.length) { $('tasks').replaceChildren(); $('outbox').replaceChildren(); status('توجد ملاحظات محفوظة لحساب آخر. سجل الدخول بالحساب نفسه أو امسح بيانات الجهاز صراحة.'); return; }
        tasks = []; queue = []; localStorage.removeItem(KEY); $('offline').checked = false;
      }
      profile = data.profile; company = data.company; tasks = data.tasks; persist(); render(); status('المهام محدثة. ' + queue.length + ' ملاحظة بانتظار المزامنة.');
    } catch (e) { status(e.message || 'تعذر الاتصال. المهام المحفوظة متاحة على هذا الجهاز.'); }
  }
  async function sync() {
    if (busy) return; busy = true; $('sync').disabled = true;
    try {
      const identity = await fetch('/work/field/data/', { credentials: 'same-origin', cache: 'no-store' });
      if (!identity.ok || !identity.headers.get('content-type').includes('application/json')) throw Error('سجل الدخول بالحساب نفسه قبل المزامنة.');
      const current = await identity.json(); if (current.profile !== profile) throw Error('الحساب أو الجهة مختلفة. لم تُرسل البيانات.');
      for (const entry of [...queue]) {
        const r = await fetch('/work/field/sync/', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(entry) });
        const data = await r.json();
        if (!r.ok) { entry.error = data.error || 'تعذرت المزامنة. احتفظ بالملاحظة وراجع السجل.'; continue; }
        queue = queue.filter(q => q.token !== entry.token); const task = tasks.find(t => rowKey(t) === rowKey(entry)); if (task) task.version = data.version; persist();
      }
      persist(); render(); status(queue.length ? 'بقيت بيانات تحتاج مراجعة؛ لم تُحذف من الصفحة.' : 'اكتملت المزامنة.'); if (!queue.length) await refresh();
    } catch (e) { status(e.message || 'تعذرت المزامنة؛ أبقِ الصفحة مفتوحة أو فعّل الحفظ المحلي.'); }
    finally { busy = false; $('sync').disabled = false; }
  }
  $('refresh').onclick = refresh; $('sync').onclick = sync;
  $('offline').onchange = () => {
    if ($('offline').checked) persist();
    else { if (queue.length && !confirm('توجد بيانات غير متزامنة. إلغاء الحفظ المحلي قد يفقدها عند إغلاق الصفحة. هل تتابع؟')) { $('offline').checked = true; return; } localStorage.removeItem(KEY); }
  };
  $('forget').onclick = () => {
    if (!confirm(queue.length ? 'ستُحذف بيانات لم تُرسل بعد. هل تريد مسح هذا الجهاز؟' : 'مسح بيانات الميدان من هذا الجهاز؟')) return;
    localStorage.removeItem(KEY); tasks = []; queue = []; profile = ''; company = ''; $('offline').checked = false; render(); status('مُسحت البيانات المحلية.');
  };
  try { const cached = JSON.parse(localStorage.getItem(KEY) || 'null'); if (cached) { ({ tasks, queue, profile, company } = cached); $('offline').checked = true; } } catch (e) { localStorage.removeItem(KEY); }
  render(); if ('serviceWorker' in navigator) navigator.serviceWorker.register('/work/field/worker.js', { scope: '/work/field/' }).catch(() => {});
  window.addEventListener('online', () => { if (queue.length) sync(); else refresh(); }); refresh();
})();
