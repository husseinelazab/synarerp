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

  function fetchRate(select) {
    if (!rateType || !select.value) return;
    var row = select.closest("tr");
    fetch("/api/item/" + select.value + "/", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var rate = field(row, "rate");
        if (rate) rate.value = data[rateType + "_rate"];
        recalc();
      });
  }

  table.addEventListener("input", recalc);
  table.addEventListener("change", function (e) {
    if (e.target.name && /-item$/.test(e.target.name)) fetchRate(e.target);
    recalc();
  });

  document.querySelector(".add-row").addEventListener("click", function () {
    var index = parseInt(totalForms.value, 10);
    var html = template.innerHTML.replace(/__prefix__/g, index);
    tbody.insertAdjacentHTML("beforeend", html);
    totalForms.value = index + 1;
    recalc();
  });

  recalc();
})();
