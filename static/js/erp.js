(function () {
  "use strict";

  // فتح السجل عند النقر على أي مكان في صف القائمة
  document.querySelectorAll("tr.clickable").forEach(function (row) {
    row.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input")) return;
      window.location = row.dataset.href;
    });
  });

  var table = document.querySelector(".child-table");
  if (!table) return;

  var prefix = table.dataset.prefix;
  var rateType = table.dataset.rateType;
  var tbody = table.querySelector("tbody");
  var totalForms = document.getElementById("id_" + prefix + "-TOTAL_FORMS");
  var template = document.getElementById("empty-row");

  function num(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function fmt(n) {
    return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function field(row, name) {
    return row.querySelector('[name$="-' + name + '"]');
  }

  function recalc() {
    var rows = tbody.querySelectorAll("tr.child-row");
    var total = 0, debit = 0, credit = 0, idx = 0;
    rows.forEach(function (row) {
      var del = field(row, "DELETE");
      var deleted = del && del.checked;
      row.style.opacity = deleted ? 0.4 : 1;
      var cell = row.querySelector(".row-idx");
      if (!deleted) idx += 1;
      if (cell) cell.textContent = deleted ? "" : idx;
      if (deleted) return;
      var qty = field(row, "qty"), rate = field(row, "rate");
      if (qty && rate) {
        var amount = num(qty.value) * num(rate.value);
        total += amount;
        var out = row.querySelector(".row-amount");
        if (out) out.textContent = fmt(amount);
      }
      var d = field(row, "debit"), c = field(row, "credit");
      if (d) debit += num(d.value);
      if (c) credit += num(c.value);
    });
    var out = table.querySelector(".child-totals");
    if (field(tbody, "debit")) {
      var diff = debit - credit;
      out.innerHTML = "إجمالي المدين: " + fmt(debit) + " &nbsp;|&nbsp; إجمالي الدائن: " + fmt(credit) +
        (Math.abs(diff) > 0.001 ? ' &nbsp;|&nbsp; <span class="neg">الفرق: ' + fmt(diff) + "</span>" : ' &nbsp;<span class="pos">✓ متوازن</span>');
    } else if (rateType) {
      out.textContent = "الإجمالي قبل الخصم والضريبة: " + fmt(total);
    }
  }

  // بيانات الصنف ووحداته (تُحفظ لتغيير الوحدة دون طلب جديد)
  var itemCache = {};
  function itemData(id) {
    if (itemCache[id]) return Promise.resolve(itemCache[id]);
    return fetch("/api/item/" + id + "/", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) { itemCache[id] = data; return data; });
  }

  function headerValue(name) {
    var el = document.getElementById("id_" + name);
    if (!el) return "";
    return el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
  }

  // سعر السطر من الخادم بقائمة الأسعار والعروض للطرف والكمية والتاريخ (كما يحسبه عند الحفظ)
  function pricedRate(id, row) {
    var uom = field(row, "uom"), qty = field(row, "qty");
    var params = new URLSearchParams({
      kind: rateType, customer: headerValue("customer"), supplier: headerValue("supplier"),
      price_list: headerValue("price_list"), date: headerValue("posting_date"), coupon: headerValue("coupon_code"),
      ignore_rules: headerValue("ignore_pricing_rules"), uom: uom ? uom.value : "", qty: qty ? qty.value : "1",
    });
    return fetch("/api/item/" + id + "/?" + params.toString(), { credentials: "same-origin" })
      .then(function (r) { return r.json(); });
  }

  function showRule(row, priced) {
    var cell = field(row, "rate") && field(row, "rate").parentNode, note = cell && cell.querySelector(".rule-note");
    if (!cell) return;
    if (!note) { note = document.createElement("div"); note.className = "rule-note muted small"; cell.appendChild(note); }
    var text = priced && priced.rule ? "🏷️ " + priced.rule : "";
    if (priced && priced.free_item && parseFloat(priced.free_qty) > 0) text += (text ? " · " : "") + "🎁 " + priced.free_item + " × " + parseFloat(priced.free_qty);
    note.textContent = text;
  }

  // سعر السطر حسب الصنف ووحدته (سعر الوحدة إن حُدد، وإلا سعر الأساسية × المعامل)
  function fetchRate(select) {
    if (!rateType || !select.value) return;
    var row = select.closest("tr");
    itemData(select.value).then(function (data) {
      var rate = field(row, "rate"), uom = field(row, "uom");
      var unit = uom && uom.value && data.uoms[uom.value];
      if (uom && uom.value && !unit) uom.value = "";  // الوحدة غير معرفة لهذا الصنف
      if (rate) rate.value = unit ? unit[rateType + "_rate"] : data[rateType + "_rate"];
      recalc();
      return pricedRate(select.value, row).then(function (res) {
        if (!res.priced || !rate) return;
        rate.value = res.priced.rate;
        row.dataset.autoRate = "1";
        showRule(row, res.priced);
        recalc();
      });
    }).catch(function () {});
  }

  table.addEventListener("input", function (e) {
    if (e.target.name && /-rate$/.test(e.target.name)) delete e.target.closest("tr").dataset.autoRate;
    recalc();
  });
  table.addEventListener("change", function (e) {
    if (e.target.name && /-item$/.test(e.target.name)) fetchRate(e.target);
    if (e.target.name && /-uom$/.test(e.target.name)) {
      var itemSelect = field(e.target.closest("tr"), "item");
      if (itemSelect) fetchRate(itemSelect);
    }
    // تغيّر الكمية قد يفعّل عرض كمية (أو يلغيه) ما دام السعر لم يُعدّل يدوياً
    if (e.target.name && /-qty$/.test(e.target.name) && e.target.closest("tr").dataset.autoRate) {
      var sel = field(e.target.closest("tr"), "item");
      if (sel) fetchRate(sel);
    }
    recalc();
  });

  document.querySelector(".add-row").addEventListener("click", function () {
    var index = parseInt(totalForms.value, 10);
    var html = template.innerHTML.replace(/__prefix__/g, index);
    tbody.insertAdjacentHTML("beforeend", html);
    totalForms.value = index + 1;
    recalc();
  });

  // إضافة الأصناف بالماسح الضوئي: يزيد كمية السطر الموجود أو يملأ سطراً فارغاً أو يضيف سطراً جديداً
  var scanInput = document.getElementById("form-scan");
  if (scanInput) {
    var scanMsg = document.getElementById("form-scan-msg");
    var activeRows = function () {
      return Array.prototype.filter.call(tbody.querySelectorAll("tr.child-row"), function (row) {
        var del = field(row, "DELETE");
        return !(del && del.checked);
      });
    };
    scanInput.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var text = scanInput.value.trim();
      scanInput.value = "";
      if (!text) return;
      var m = /^(\d+(?:\.\d+)?)\s*[*xX×]\s*(.+)$/.exec(text), qty = m ? parseFloat(m[1]) : 1, code = m ? m[2] : text;
      fetch("/api/item/lookup/?code=" + encodeURIComponent(code), { credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          if (!res.ok) { scanMsg.className = "scan-msg text-danger"; scanMsg.textContent = "✗ " + res.data.error + " (" + code + ")"; return; }
          if (res.data.weighed) qty = qty * parseFloat(res.data.qty);  // باركود ميزان: الكمية = الوزن
          qty = Math.round(qty * 1000) / 1000;
          var unitId = res.data.uom ? String(res.data.uom) : "";
          var id = String(res.data.id), rows = activeRows(), row = null;
          rows.forEach(function (r) {
            var s = field(r, "item"), u = field(r, "uom");
            if (!row && s && s.value === id && (!u || (u.value || "") === unitId)) row = r;
          });
          if (row) {
            var q = field(row, "qty");
            if (q) q.value = Math.round((num(q.value) + qty) * 1000) / 1000;
          } else {
            rows.forEach(function (r) { var s = field(r, "item"); if (!row && s && !s.value) row = r; });
            if (!row) { document.querySelector(".add-row").click(); row = tbody.querySelector("tr.child-row:last-child"); }
            var select = field(row, "item");
            if (window.SynarSearch) window.SynarSearch.setValue(select, id, (res.data.code ? res.data.code + " - " : "") + res.data.name); else select.value = id;
            select.dispatchEvent(new Event("change"));  // يحدّث اسم الصنف الظاهر في مربع البحث
            if (select.value !== id) { scanMsg.className = "scan-msg text-danger"; scanMsg.textContent = "✗ الصنف غير متاح في هذا المستند"; return; }
            var q2 = field(row, "qty"), u2 = field(row, "uom");
            if (q2) q2.value = qty;
            if (u2) u2.value = unitId;
            fetchRate(select);
          }
          scanMsg.className = "scan-msg ok";
          scanMsg.textContent = "✓ " + res.data.name + (res.data.uom_name ? " (" + res.data.uom_name + ")" : "");
          recalc();
        });
    });
  }

  recalc();
})();
