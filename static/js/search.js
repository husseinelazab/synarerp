/* البحث في كل الواجهة:
 * 1) القوائم المنسدلة الطويلة تصبح قابلة للبحث بالكتابة (بالاسم أو الرقم أو الكود).
 * 2) كل جدول تقرير طويل يحصل على مربع «بحث في الجدول» يرشّح صفوفه فوراً.
 * البحث يتجاهل التشكيل ويوحّد الهمزات والتاء المربوطة والألف المقصورة.
 */
(function () {
  "use strict";

  function norm(text) {
    return (text || "").toString().toLowerCase()
      .replace(/[ً-ْٰـ]/g, "")
      .replace(/[أإآٱ]/g, "ا").replace(/ة/g, "ه").replace(/[ىئ]/g, "ي").replace(/ؤ/g, "و")
      .replace(/[٠-٩]/g, function (d) { return "٠١٢٣٤٥٦٧٨٩".indexOf(d); })
      .replace(/\s+/g, " ").trim();
  }

  // جذع تقريبي حتى يطابق المفرد الجمع (كفالة ← الكفالات)، مثل البحث على الخادم
  function stem(w) {
    var pre = ["وال", "بال", "لل", "ال"], suf = ["ات", "ون", "ين", "يه", "ه"], i;
    for (i = 0; i < pre.length; i++) if (w.indexOf(pre[i]) === 0 && w.length - pre[i].length >= 3) { w = w.slice(pre[i].length); break; }
    for (i = 0; i < suf.length; i++) if (w.slice(-suf[i].length) === suf[i] && w.length - suf[i].length >= 3) return w.slice(0, -suf[i].length);
    return w;
  }

  function matches(text, query) {
    var t = norm(text);
    return norm(query).split(" ").every(function (w) { return !w || t.indexOf(stem(w)) !== -1; });
  }

  // ---------- القوائم المنسدلة القابلة للبحث ----------
  var MIN_OPTIONS = 12;

  function enhanceSelect(select) {
    if (select.dataset.searchable || select.multiple || select.options.length < MIN_OPTIONS ||
        select.closest(".no-search") || select.closest("template")) return;
    select.dataset.searchable = "1";
    var box = document.createElement("div");
    box.className = "combo";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "form-control combo-input";
    input.autocomplete = "off";
    input.placeholder = "اكتب للبحث…";
    var list = document.createElement("div");
    list.className = "combo-list";
    list.hidden = true;
    select.parentNode.insertBefore(box, select);
    box.appendChild(input);
    box.appendChild(list);
    box.appendChild(select);
    select.classList.add("combo-native");
    select.tabIndex = -1;

    var active = -1, shown = [];

    function label() {
      var opt = select.options[select.selectedIndex];
      input.value = opt && opt.value ? opt.text : "";
    }

    function render(query) {
      list.innerHTML = "";
      shown = [];
      for (var i = 0; i < select.options.length && shown.length < 200; i++) {
        var opt = select.options[i];
        if (!opt.value && query) continue;
        if (query && !matches(opt.text, query)) continue;
        var div = document.createElement("div");
        div.className = "combo-item" + (opt.value === select.value ? " selected" : "");
        div.textContent = opt.text;
        div.dataset.index = i;
        list.appendChild(div);
        shown.push(div);
      }
      if (!shown.length) {
        var empty = document.createElement("div");
        empty.className = "combo-empty";
        empty.textContent = "لا نتائج";
        list.appendChild(empty);
      }
      active = shown.length ? 0 : -1;
      list.hidden = false;
      place();
      highlight();
    }

    // القائمة مثبّتة على الشاشة حتى لا تقصّها الجداول القابلة للتمرير أفقياً
    function place() {
      var r = input.getBoundingClientRect(), vw = document.documentElement.clientWidth;
      var width = Math.min(Math.max(r.width, 220), vw - 16);
      var right = Math.min(Math.max(r.right, width + 8), vw - 8);  // محاذاة يمين الحقل (واجهة عربية)
      list.style.width = width + "px";
      list.style.left = (right - width) + "px";
      var below = window.innerHeight - r.bottom;
      if (below < 200 && r.top > below) { list.style.top = ""; list.style.bottom = (window.innerHeight - r.top + 2) + "px"; }
      else { list.style.bottom = ""; list.style.top = (r.bottom + 2) + "px"; }
    }

    function highlight() {
      shown.forEach(function (el, i) { el.classList.toggle("active", i === active); });
      if (shown[active]) shown[active].scrollIntoView({ block: "nearest" });
    }

    function choose(index) {
      select.selectedIndex = index;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      label();
      list.hidden = true;
    }

    input.addEventListener("focus", function () { input.select(); render(""); });
    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { active = Math.min(active + 1, shown.length - 1); highlight(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); highlight(); e.preventDefault(); }
      else if (e.key === "Enter") { if (!list.hidden && shown[active]) { choose(+shown[active].dataset.index); e.preventDefault(); } }
      else if (e.key === "Escape") { list.hidden = true; label(); }
    });
    list.addEventListener("mousedown", function (e) {
      var item = e.target.closest(".combo-item");
      if (item) { choose(+item.dataset.index); e.preventDefault(); }
    });
    window.addEventListener("scroll", function (e) {
      if (!list.hidden && e.target !== list) place();
    }, true);
    window.addEventListener("resize", function () { if (!list.hidden) place(); });
    input.addEventListener("blur", function () { setTimeout(function () { list.hidden = true; label(); }, 120); });
    select.addEventListener("change", label);  // تغيير القيمة برمجياً (المسح الضوئي، التعبئة التلقائية)
    if (select.required) input.required = true;
    label();
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll("select.form-select").forEach(enhanceSelect);
  }

  // ---------- البحث داخل جداول التقارير ----------
  var MIN_ROWS = 8;

  function addTableFilter(table) {
    if (table.dataset.filterable || table.closest(".no-search") || table.classList.contains("child-table")) return;
    var body = table.tBodies[0];
    if (!body || body.rows.length < MIN_ROWS) return;
    table.dataset.filterable = "1";
    var anchor = table.closest(".table-wrap") || table;
    var bar = document.createElement("div");
    bar.className = "table-search";
    var input = document.createElement("input");
    input.type = "search";
    input.className = "form-control";
    input.placeholder = "بحث في الجدول… (اسم، رقم، مبلغ)";
    var count = document.createElement("span");
    count.className = "muted small";
    bar.appendChild(input);
    bar.appendChild(count);
    anchor.parentNode.insertBefore(bar, anchor);
    input.addEventListener("input", function () {
      var q = input.value, visible = 0, rows = body.rows;
      for (var i = 0; i < rows.length; i++) {
        var ok = !q || matches(rows[i].textContent, q);
        rows[i].style.display = ok ? "" : "none";
        if (ok) visible++;
      }
      count.textContent = q ? visible + " من " + rows.length : "";
    });
  }

  function filterAll(root) {
    (root || document).querySelectorAll(".content table.table").forEach(addTableFilter);
    // القادم من البحث العام (مثل دليل الحسابات) يجد الجدول مرشّحاً بكلمة البحث
    var q = new URLSearchParams(location.search).get("q");
    var boxes = document.querySelectorAll(".table-search input");
    if (q && boxes.length === 1 && !document.querySelector(".content form input[name=q]")) {
      boxes[0].value = q;
      boxes[0].dispatchEvent(new Event("input"));
    }
  }

  // ---------- البحث العام: اقتراحات فورية أثناء الكتابة ----------
  function globalSuggest() {
    var form = document.querySelector(".global-search[data-suggest]");
    if (!form) return;
    var input = form.querySelector("input[name=q]"), panel = form.querySelector(".suggest");
    var timer = null, seq = 0, links = [], active = -1;

    function close() { panel.hidden = true; active = -1; }
    function highlight() {
      links.forEach(function (a, i) { a.classList.toggle("active", i === active); });
      if (links[active]) links[active].scrollIntoView({ block: "nearest" });
    }
    function add(parent, url, title, meta, cls) {
      var a = document.createElement("a");
      a.href = url;
      a.className = "suggest-item" + (cls ? " " + cls : "");
      var t = document.createElement("span"); t.textContent = title; a.appendChild(t);
      if (meta) { var m = document.createElement("small"); m.textContent = meta; a.appendChild(m); }
      parent.appendChild(a);
      links.push(a);
    }
    function render(data) {
      panel.innerHTML = ""; links = []; active = -1;
      (data.groups || []).forEach(function (g) {
        var h = document.createElement("div"); h.className = "suggest-head"; h.textContent = g.title; panel.appendChild(h);
        g.items.forEach(function (it) { add(panel, it.url, it.title, it.meta, it.kind); });
      });
      if (!links.length) {
        var e = document.createElement("div"); e.className = "suggest-empty";
        e.textContent = "لا نتائج لـ «" + input.value.trim() + "»"; panel.appendChild(e);
      }
      add(panel, form.action + "?q=" + encodeURIComponent(input.value.trim()), "كل نتائج «" + input.value.trim() + "»",
          "Enter", "all");
      panel.hidden = false;
    }
    function fetchSuggest() {
      var q = input.value.trim();
      if (q.length < 2) { close(); return; }
      var mine = ++seq;
      fetch(form.dataset.suggest + "?q=" + encodeURIComponent(q), { headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.json() : { groups: [] }; })
        .then(function (data) { if (mine === seq && document.activeElement === input) render(data); })
        .catch(function () {});
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(fetchSuggest, 180); });
    input.addEventListener("focus", function () { if (input.value.trim().length >= 2) fetchSuggest(); });
    input.addEventListener("keydown", function (e) {
      if (panel.hidden) return;
      if (e.key === "ArrowDown") { active = Math.min(active + 1, links.length - 1); highlight(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, -1); highlight(); e.preventDefault(); }
      else if (e.key === "Enter" && links[active]) { location.href = links[active].href; e.preventDefault(); }
      else if (e.key === "Escape") { close(); }
    });
    document.addEventListener("click", function (e) { if (!form.contains(e.target)) close(); });
  }

  // ---------- تصفية القائمة الجانبية ----------
  function navFilter() {
    var input = document.getElementById("nav-filter"), sidebar = document.getElementById("sidebar");
    if (!input) return;
    var modules = Array.prototype.slice.call(sidebar.querySelectorAll(".nav-module"));
    var initial = modules.map(function (d) { return d.open; });
    input.addEventListener("input", function () {
      var q = input.value.trim(), any = false;
      sidebar.classList.toggle("filtering", !!q);
      modules.forEach(function (mod, i) {
        var title = mod.querySelector("summary span").textContent, moduleHit = q && matches(title, q), hits = 0;
        mod.querySelectorAll(".nav-item").forEach(function (a) {
          var group = a.closest(".nav-group"), heading = group ? group.querySelector("summary").textContent : "";
          var show = !q || moduleHit || matches(a.textContent + " " + heading, q);
          a.hidden = !show;
          if (show) hits++;
        });
        mod.querySelectorAll(".nav-group").forEach(function (g) {
          var visible = g.querySelectorAll(".nav-item:not([hidden])").length;
          g.hidden = q && !visible; if (q && visible) g.open = true;
        });
        mod.querySelectorAll(".nav-heading").forEach(function (h) {
          var n = h.nextElementSibling, visible = false;
          while (n && n.classList.contains("nav-item")) { if (!n.hidden) visible = true; n = n.nextElementSibling; }
          h.hidden = q && !visible;
        });
        mod.hidden = q && !hits;
        mod.open = q ? hits > 0 : initial[i];
        if (q && hits) any = true;
      });
      sidebar.querySelector(".nav-empty").hidden = !q || any;
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        var first = sidebar.querySelector(".nav-item:not([hidden])");
        if (first && input.value.trim()) { location.href = first.href; e.preventDefault(); }
      } else if (e.key === "Escape") { input.value = ""; input.dispatchEvent(new Event("input")); }
    });
  }

  function init() {
    // «/» أو Ctrl+K ينقل التركيز إلى البحث العام من أي صفحة
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      var palette = (e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K");
      if (!palette && (e.key !== "/" || e.ctrlKey || e.metaKey || tag === "input" || tag === "textarea" ||
          tag === "select" || e.target.isContentEditable)) return;
      var box = document.querySelector(".global-search input");
      if (box) { box.focus(); box.select(); e.preventDefault(); }
    });
    globalSuggest();
    navFilter();
    enhanceAll();
    filterAll();
    // الأسطر الجديدة في جداول النماذج (إضافة سطر، الماسح الضوئي)
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        m.addedNodes.forEach(function (n) { if (n.nodeType === 1) enhanceAll(n); });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  window.SynarSearch = { norm: norm, matches: matches, enhance: enhanceAll };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
