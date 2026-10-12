/* البحث في كل الواجهة:
 * 1) القوائم المنسدلة الطويلة تصبح قابلة للبحث بالكتابة (بالاسم أو الرقم أو الكود).
 * 2) كل جدول تقرير طويل يحصل على مربع «بحث في الجدول» يرشّح صفوفه فوراً.
 * البحث يتجاهل التشكيل ويوحّد الهمزات والتاء المربوطة والألف المقصورة.
 */
(function () {
  "use strict";

  function norm(text) {
    return (text || "").toString().toLowerCase()
      .replace(/[ً-ْـ]/g, "")
      .replace(/[أإآٱ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي")
      .replace(/[٠-٩]/g, function (d) { return "٠١٢٣٤٥٦٧٨٩".indexOf(d); })
      .replace(/\s+/g, " ").trim();
  }

  function matches(text, query) {
    var t = norm(text);
    return norm(query).split(" ").every(function (w) { return !w || t.indexOf(w) !== -1; });
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

  function init() {
    // «/» ينقل التركيز إلى البحث العام من أي صفحة
    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      if (e.key !== "/" || e.ctrlKey || e.metaKey || tag === "input" || tag === "textarea" || tag === "select" ||
          e.target.isContentEditable) return;
      var box = document.querySelector(".global-search input");
      if (box) { box.focus(); box.select(); e.preventDefault(); }
    });
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
