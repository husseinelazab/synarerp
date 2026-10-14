/* اتجاه الطباعة: طولي أو عرضي حسب حاجة التقرير.
 * - زر «طباعة» يختار تلقائياً: عرضي إذا كان في الصفحة جدول عريض (أعمدة كثيرة أو أعرض من صفحة طولية)، وإلا طولي.
 * - يمكن تثبيت الاتجاه لكل صفحة (طولي/عرضي)، ويُحفظ في المتصفح ويُطبّق أيضاً على Ctrl+P وأزرار الطباعة الأخرى.
 */
(function () {
  "use strict";

  var WIDE_COLUMNS = 8;
  var PORTRAIT_PX = 760; // عرض محتوى صفحة A4 طولية تقريباً
  var key = "print-orientation:" + location.pathname;

  function stored() {
    try { return localStorage.getItem(key) || ""; } catch (e) { return ""; }
  }
  function store(value) {
    try { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); } catch (e) { /* تصفح خاص */ }
  }

  function tables() {
    return Array.prototype.slice.call(document.querySelectorAll(".content table"));
  }

  function auto() {
    var wide = tables().some(function (t) {
      var row = t.tHead && t.tHead.rows.length ? t.tHead.rows[t.tHead.rows.length - 1] : t.rows[0];
      var columns = 0;
      if (row) for (var i = 0; i < row.cells.length; i++) columns += row.cells[i].colSpan || 1;
      return columns >= WIDE_COLUMNS || t.scrollWidth > PORTRAIT_PX * 1.15;
    });
    return wide ? "landscape" : "portrait";
  }

  function apply(orientation) {
    var style = document.getElementById("print-orientation");
    if (!style) {
      style = document.createElement("style");
      style.id = "print-orientation";
      document.head.appendChild(style);
    }
    style.textContent = "@page { size: A4 " + orientation + "; }";
    document.documentElement.dataset.printOrientation = orientation;
  }

  // التواريخ والأرقام القصيرة لا تنكسر على سطرين في الطباعة (2026-10-01 مثلاً)
  var COMPACT = /^[\d\s,.\-\/:()%٫٬\u0660-\u0669\u20c1]+$/;
  function keepCompact() {
    document.querySelectorAll(".content td, .content th").forEach(function (cell) {
      var text = cell.textContent.trim();
      if (text && text.length <= 22 && COMPACT.test(text)) cell.classList.add("nowrap-print");
    });
  }

  function current() { return stored() || auto(); }

  function mark(tools) {
    var chosen = stored();
    tools.querySelectorAll("[data-orientation]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.orientation === (chosen || auto()));
      b.classList.toggle("pinned", b.dataset.orientation === chosen);
    });
  }

  function init() {
    var tools = document.querySelector(".print-tools");
    if (tools && tables().length && location.pathname !== "/") {
      tools.hidden = false;
      mark(tools);
      tools.addEventListener("click", function (e) {
        var button = e.target.closest("button");
        if (!button) return;
        if (button.dataset.orientation) {
          // النقر على الاتجاه المثبت يلغي التثبيت ويعود للاختيار التلقائي
          store(stored() === button.dataset.orientation ? "" : button.dataset.orientation);
          mark(tools);
          keepCompact();
          apply(current());
          window.print();
        } else if (button.dataset.print) {
          keepCompact();
          apply(current());
          window.print();
        }
      });
    }
    // Ctrl+P وأزرار «طباعة» الموجودة في التقارير تأخذ نفس الاتجاه
    window.addEventListener("beforeprint", function () { keepCompact(); apply(current()); });
  }

  window.SynarPrint = { auto: auto, apply: apply };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
