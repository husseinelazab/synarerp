(function () {
  "use strict";

  var config = JSON.parse(document.getElementById("pos-config").textContent);
  var csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  var items = [], byId = {}, cart = [], group = "", busy = false;

  var $ = function (id) { return document.getElementById(id); };
  var search = $("search"), grid = $("grid"), cartEl = $("cart"), discountEl = $("discount");

  function round2(n) { return Math.round((n + Number.EPSILON) * 100) / 100; }
  function fmt(n) { return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function num(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

  // نفس معادلات الخادم لحساب الإجماليات
  function totals() {
    var total = 0;
    cart.forEach(function (l) { total += round2(l.qty * l.rate); });
    total = round2(total);
    var discount = discountEl ? num(discountEl.value) : 0;
    var rate = config.taxRate, net, tax, grand;
    if (config.pricesIncludeTax) {
      grand = round2(total - discount);
      tax = round2(grand * rate / (100 + rate));
      net = round2(grand - tax);
    } else {
      net = round2(total - discount);
      tax = round2(net * rate / 100);
      grand = round2(net + tax);
    }
    return { total: total, discount: discount, net: net, tax: tax, grand: grand };
  }

  function loadItems() {
    return fetch(config.itemsUrl, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .catch(function () { return { items: items }; })  // دون اتصال: تبقى القائمة الحالية
      .then(function (data) {
        items = data.items || [];
        byId = {};
        items.forEach(function (i) { byId[i.id] = i; });
        renderGrid();
      });
  }

  function renderGrid() {
    var q = search.value.trim().toLowerCase();
    var list = items.filter(function (i) {
      if (group && String(i.group) !== group) return false;
      if (!q) return true;
      return i.name.toLowerCase().indexOf(q) >= 0 || i.code.toLowerCase().indexOf(q) >= 0 || (i.barcode && i.barcode.indexOf(q) >= 0);
    });
    if (!list.length) { grid.innerHTML = '<div class="muted">لا توجد أصناف مطابقة</div>'; return; }
    grid.innerHTML = list.slice(0, 300).map(function (i) {
      var out = i.stock && i.qty <= 0;
      return '<button class="product' + (out ? " out" : "") + '" data-id="' + i.id + '">' +
        '<span class="product-name">' + esc(i.name) + '</span>' +
        '<span class="product-code">' + esc(i.code) + '</span>' +
        '<span class="product-price">' + fmt(i.rate) + config.currencyHtml + '</span>' +
        (i.stock ? '<span class="product-qty">المتوفر: ' + i.qty + " " + esc(i.uom) + "</span>" : '<span class="product-qty">خدمة</span>') +
        "</button>";
    }).join("");
  }

  function addToCart(id, qty) {
    var item = byId[id];
    if (!item) return;
    var line = cart.find(function (l) { return l.id === item.id; });
    if (line) line.qty = round2(line.qty + (qty || 1));
    else cart.push({ id: item.id, name: item.name, rate: item.rate, qty: qty || 1 });
    renderCart();
  }

  function renderCart() {
    if (!cart.length) {
      cartEl.innerHTML = '<div class="cart-empty">السلة فارغة — اختر صنفاً أو امسح الباركود</div>';
    } else {
      cartEl.innerHTML = cart.map(function (l, idx) {
        var rate = config.allowRateChange
          ? '<input type="number" step="0.01" min="0" value="' + l.rate + '" data-rate="' + idx + '">'
          : fmt(l.rate);
        return '<div class="cart-line">' +
          '<div><div class="cart-line-name">' + esc(l.name) + '</div><div class="cart-line-rate">السعر: ' + rate + "</div></div>" +
          '<div class="qty-box"><button data-dec="' + idx + '">−</button><input type="number" step="any" min="0" value="' + l.qty + '" data-qty="' + idx + '"><button data-inc="' + idx + '">+</button></div>' +
          '<div style="display:flex;align-items:center;gap:4px"><span class="cart-line-amount">' + fmt(round2(l.qty * l.rate)) + '</span><button class="cart-line-remove" data-remove="' + idx + '" title="حذف">×</button></div>' +
          "</div>";
      }).join("");
    }
    renderTotals();
  }

  function renderTotals() {
    var t = totals();
    $("t-total").textContent = fmt(t.total);
    $("t-net").textContent = fmt(t.net);
    $("t-tax").textContent = fmt(t.tax);
    $("t-grand").innerHTML = fmt(t.grand) + config.currencyHtml;
  }

  // البحث ومسح الباركود: Enter يضيف الصنف المطابق تماماً للباركود أو الكود
  search.addEventListener("input", renderGrid);
  search.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var q = search.value.trim();
    if (!q) return;
    var match = items.find(function (i) { return i.barcode === q || i.code.toLowerCase() === q.toLowerCase(); });
    if (!match) {
      var visible = grid.querySelectorAll(".product");
      if (visible.length === 1) match = byId[visible[0].dataset.id];
    }
    if (match) { addToCart(match.id); search.value = ""; renderGrid(); }
  });

  $("groups").addEventListener("click", function (e) {
    var chip = e.target.closest(".chip");
    if (!chip) return;
    document.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
    chip.classList.add("active");
    group = chip.dataset.group;
    renderGrid();
  });

  grid.addEventListener("click", function (e) {
    var card = e.target.closest(".product");
    if (card) { addToCart(parseInt(card.dataset.id, 10)); search.focus(); }
  });

  cartEl.addEventListener("click", function (e) {
    var t = e.target;
    if (t.dataset.inc) { cart[t.dataset.inc].qty = round2(cart[t.dataset.inc].qty + 1); }
    else if (t.dataset.dec) {
      var l = cart[t.dataset.dec];
      l.qty = round2(l.qty - 1);
      if (l.qty <= 0) cart.splice(t.dataset.dec, 1);
    } else if (t.dataset.remove) { cart.splice(t.dataset.remove, 1); }
    else return;
    renderCart();
  });

  cartEl.addEventListener("change", function (e) {
    var t = e.target;
    if (t.dataset.qty !== undefined) {
      var q = num(t.value);
      if (q <= 0) cart.splice(t.dataset.qty, 1); else cart[t.dataset.qty].qty = q;
    } else if (t.dataset.rate !== undefined) {
      cart[t.dataset.rate].rate = Math.max(num(t.value), 0);
    }
    renderCart();
  });

  if (discountEl) discountEl.addEventListener("input", renderTotals);

  $("clear").addEventListener("click", function () {
    if (cart.length && confirm("تفريغ السلة؟")) resetSale();
  });

  function resetSale() {
    cart = [];
    if (discountEl) discountEl.value = 0;
    $("customer").value = config.defaultCustomer;
    renderCart();
    search.focus();
  }

  // نافذة الدفع
  var payModal = $("pay-modal"), cashEl = $("m-cash"), cardEl = $("m-card"), errEl = $("m-error");

  function openPay() {
    if (!cart.length) return;
    var t = totals();
    if (config.allowDiscount && t.discount > t.total * config.maxDiscountPercent / 100 + 0.001) {
      alert("الخصم يتجاوز الحد المسموح (" + config.maxDiscountPercent + "%).");
      return;
    }
    $("m-grand").innerHTML = fmt(t.grand) + config.currencyHtml;
    cashEl.value = t.grand.toFixed(2);
    cardEl.value = 0;
    errEl.hidden = true;
    payModal.hidden = false;
    updateChange();
    cashEl.select();
  }

  function updateChange() {
    var change = round2(num(cashEl.value) + num(cardEl.value) - totals().grand);
    var label = $("m-change-label"), out = $("m-change");
    label.textContent = change < 0 ? "المتبقي على العميل: " : "الباقي للعميل: ";
    out.innerHTML = fmt(Math.abs(change)) + config.currencyHtml;
    out.classList.toggle("neg", change < 0);
    $("m-confirm").disabled = change < 0;
  }

  cashEl.addEventListener("input", updateChange);
  cardEl.addEventListener("input", updateChange);
  $("pay").addEventListener("click", openPay);
  $("m-cancel").addEventListener("click", function () { payModal.hidden = true; });
  document.querySelector(".quick-pay").addEventListener("click", function (e) {
    var q = e.target.dataset.quick;
    if (!q) return;
    var grand = totals().grand;
    if (q === "exact") { cashEl.value = grand.toFixed(2); cardEl.value = 0; }
    else if (q === "card") { cardEl.value = grand.toFixed(2); cashEl.value = 0; }
    else { cashEl.value = num(q).toFixed(2); }
    updateChange();
  });

  $("m-confirm").addEventListener("click", function () {
    if (busy) return;
    busy = true;
    errEl.hidden = true;
    var t = totals();
    if (!navigator.onLine) { saveOffline(t); busy = false; return; }
    fetch(config.checkoutUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({
        customer: $("customer").value,
        discount: t.discount,
        paid_cash: num(cashEl.value),
        paid_card: num(cardEl.value),
        items: cart.map(function (l) { return { item: l.id, qty: l.qty, rate: l.rate }; }),
      }),
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) { errEl.textContent = res.data.error || "تعذر إتمام البيع."; errEl.hidden = false; return; }
        payModal.hidden = true;
        $("d-title").textContent = "✓ تمت عملية البيع";
        $("d-change-label").textContent = "الباقي للعميل: ";
        $("d-name").textContent = res.data.name;
        $("d-change").innerHTML = fmt(num(res.data.change)) + config.currencyHtml;
        $("d-print").href = res.data.receipt_url + "?print=1";
        $("d-print").onclick = null;
        $("done-modal").hidden = false;
        $("d-new").focus();
        loadItems();
      })
      .catch(function () { saveOffline(t); })  // انقطع الاتصال أثناء الإرسال: يُحفظ البيع في الجهاز
      .finally(function () { busy = false; });
  });

  $("d-new").addEventListener("click", function () { $("done-modal").hidden = true; resetSale(); });

  // ---------- العمل دون اتصال ----------
  function readQueue() {
    try { return JSON.parse(localStorage.getItem(config.queueKey) || "[]"); } catch (e) { return []; }
  }
  function writeQueue(q) { localStorage.setItem(config.queueKey, JSON.stringify(q)); renderPending(); }
  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }

  function saveOffline(t) {
    var sale = {
      client_id: newId(), created_at: new Date().toISOString(), customer: $("customer").value,
      customer_name: $("customer").selectedOptions[0] ? $("customer").selectedOptions[0].text : "",
      discount: t.discount, paid_cash: num(cashEl.value), paid_card: num(cardEl.value),
      items: cart.map(function (l) { return { item: l.id, qty: l.qty, rate: l.rate, name: l.name }; }),
      totals: t, status: "pending", error: "",
    };
    var q = readQueue();
    q.push(sale);
    writeQueue(q);
    // خصم الكميات محلياً حتى يظهر الرصيد التقريبي
    cart.forEach(function (l) { var i = byId[l.id]; if (i && i.stock) i.qty = round2(i.qty - l.qty); });
    renderGrid();
    payModal.hidden = true;
    var change = round2(sale.paid_cash + sale.paid_card - t.grand);
    $("d-title").textContent = "✓ حُفظ البيع دون اتصال";
    $("d-change-label").textContent = "الباقي للعميل: ";
    $("d-name").textContent = "مؤقت " + sale.client_id.slice(0, 8).toUpperCase();
    $("d-change").innerHTML = fmt(Math.max(change, 0)) + config.currencyHtml;
    $("d-print").removeAttribute("href");
    $("d-print").onclick = function (e) { e.preventDefault(); printProvisional(sale); };
    $("done-modal").hidden = false;
    $("d-new").focus();
  }

  function printProvisional(sale) {
    var t = sale.totals, rows = sale.items.map(function (l) {
      return "<tr><td>" + esc(l.name) + "</td><td>" + l.qty + "</td><td>" + fmt(round2(l.qty * l.rate)) + "</td></tr>";
    }).join("");
    var w = window.open("", "_blank", "width=380,height=640");
    if (!w) return;
    w.document.write('<!doctype html><html dir="rtl" lang="ar"><head><meta charset="utf-8"><title>إيصال مؤقت</title>' +
      '<style>body{font-family:Tahoma,sans-serif;font-size:13px;margin:12px}table{width:100%;border-collapse:collapse}' +
      'td,th{padding:3px;border-bottom:1px dashed #999;text-align:right}.c{text-align:center}.b{font-weight:bold}</style></head><body>' +
      '<div class="c b">' + esc(config.companyName) + "</div>" +
      (config.taxId ? '<div class="c">الرقم الضريبي: ' + esc(config.taxId) + "</div>" : "") +
      '<div class="c b" style="margin:8px 0">إيصال مؤقت — بيع دون اتصال</div>' +
      "<div>المرجع: " + sale.client_id.slice(0, 8).toUpperCase() + "</div><div>الوقت: " + new Date(sale.created_at).toLocaleString("ar-SA") +
      "</div><div>الكاشير: " + esc(config.cashier) + "</div>" +
      "<table><thead><tr><th>الصنف</th><th>الكمية</th><th>المبلغ</th></tr></thead><tbody>" + rows + "</tbody></table>" +
      "<p>الإجمالي قبل الضريبة: " + fmt(t.net) + "<br>الضريبة: " + fmt(t.tax) + '<br><span class="b">الإجمالي: ' + fmt(t.grand) + "</span></p>" +
      '<p style="font-size:11px">هذا إيصال مؤقت. تصدر الفاتورة الضريبية المبسطة برمز QR عند عودة الاتصال، ويمكن طباعتها من ملخص الوردية.</p>' +
      "<script>window.print()<\/script></body></html>");
    w.document.close();
  }

  var syncing = false;
  function syncQueue(force) {
    var q = readQueue();
    var toSend = q.filter(function (s) { return force || s.status !== "failed"; });
    if (syncing || !toSend.length || !navigator.onLine) return;
    syncing = true;
    fetch(config.syncUrl, {
      method: "POST", credentials: "same-origin", redirect: "error",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ sales: toSend }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) { setStatus(true, res.data.error || "تعذرت المزامنة"); return; }
        var results = {};
        (res.data.results || []).forEach(function (x) { results[x.client_id] = x; });
        var remaining = readQueue().filter(function (s) {
          var r = results[s.client_id];
          if (!r) return true;
          if (r.ok) return false;
          s.status = "failed"; s.error = r.error;
          return true;
        });
        writeQueue(remaining);
        loadItems();
      })
      .catch(function () { /* ما زال الاتصال منقطعاً أو انتهت الجلسة؛ تبقى المبيعات محفوظة */ })
      .finally(function () { syncing = false; });
  }

  function renderPending() {
    var q = readQueue();
    $("pending-btn").hidden = !q.length;
    $("pending-count").textContent = q.length;
    $("pending-list").innerHTML = q.map(function (s, idx) {
      return "<tr><td>" + new Date(s.created_at).toLocaleString("ar-SA") + "</td><td>" + fmt(s.totals.grand) + "</td><td>" +
        (s.status === "failed" ? '<span class="text-danger">' + esc(s.error) + "</span>" : "بانتظار الاتصال") + "</td><td>" +
        (s.status === "failed" ? '<button class="btn btn-sm" data-discard="' + idx + '">حذف</button>' : "") + "</td></tr>";
    }).join("");
  }

  function setStatus(online, message) {
    var el = $("net-status");
    el.textContent = online ? (message ? "● " + message : "● متصل") : "● دون اتصال — المبيعات تُحفظ في الجهاز";
    el.classList.toggle("offline", !online);
  }

  window.addEventListener("online", function () { setStatus(true); syncQueue(); });
  window.addEventListener("offline", function () { setStatus(false); });
  $("pending-btn").addEventListener("click", function () { renderPending(); $("pending-modal").hidden = false; });
  $("pending-close").addEventListener("click", function () { $("pending-modal").hidden = true; });
  $("pending-sync").addEventListener("click", function () { syncQueue(true); });
  $("pending-list").addEventListener("click", function (e) {
    var idx = e.target.dataset.discard;
    if (idx === undefined || !confirm("حذف هذا البيع من الجهاز نهائياً؟ تأكد من تسويته يدوياً أولاً.")) return;
    var q = readQueue();
    q.splice(parseInt(idx, 10), 1);
    writeQueue(q);
  });
  setInterval(function () { syncQueue(false); }, 20000);
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register(config.swUrl, { scope: "/pos/" }).catch(function () {});
  }

  // ---------- المرتجعات ----------
  var returnModal = $("return-modal"), rError = $("r-error"), rInvoice = null;

  function openReturn() {
    if (!navigator.onLine) { alert("المرتجعات تحتاج اتصالاً بالخادم للتحقق من الفاتورة الأصلية."); return; }
    returnModal.hidden = false;
    $("r-body").hidden = true;
    $("r-confirm").disabled = true;
    rError.hidden = true;
    $("r-invoice").value = "";
    $("r-invoice").focus();
  }

  function returnLines() {
    return Array.prototype.map.call(document.querySelectorAll("#r-items input"), function (input) {
      return { item: parseInt(input.dataset.item, 10), qty: num(input.value), rate: num(input.dataset.rate), max: num(input.max) };
    });
  }

  function updateReturnTotal() {
    var total = 0, valid = true, any = false;
    returnLines().forEach(function (l) {
      if (l.qty < 0 || l.qty > l.max) valid = false;
      if (l.qty > 0) any = true;
      total += l.qty * l.rate;
    });
    $("r-total").textContent = fmt(round2(total * (rInvoice ? rInvoice.factor : 0)));
    $("r-confirm").disabled = !(valid && any);
  }

  function loadInvoice() {
    var name = $("r-invoice").value.trim();
    if (!name) return;
    rError.hidden = true;
    fetch(config.invoiceUrl + "?name=" + encodeURIComponent(name), { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) { rError.textContent = res.data.error; rError.hidden = false; $("r-body").hidden = true; return; }
        rInvoice = res.data;
        $("r-info").textContent = "العميل: " + rInvoice.customer + " · التاريخ: " + rInvoice.date + " · الإجمالي: " + fmt(num(rInvoice.grand_total));
        $("r-items").innerHTML = rInvoice.items.map(function (i) {
          return "<tr><td>" + esc(i.name) + "</td><td>" + i.sold + "</td><td>" + i.returnable + "</td><td>" + fmt(i.rate) +
            '</td><td><input type="number" class="form-control" min="0" step="any" max="' + i.returnable + '" value="0" data-item="' + i.item + '" data-rate="' + i.rate + '"></td></tr>';
        }).join("");
        $("r-method").value = num(rInvoice.paid_card) > 0 ? "card" : "cash";
        $("r-body").hidden = false;
        updateReturnTotal();
      });
  }

  $("open-return").addEventListener("click", openReturn);
  $("r-cancel").addEventListener("click", function () { returnModal.hidden = true; search.focus(); });
  $("r-load").addEventListener("click", loadInvoice);
  $("r-invoice").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); loadInvoice(); } });
  $("r-items").addEventListener("input", updateReturnTotal);

  $("r-confirm").addEventListener("click", function () {
    if (busy || !rInvoice) return;
    busy = true;
    var lines = returnLines().filter(function (l) { return l.qty > 0; });
    fetch(config.returnUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({
        invoice: rInvoice.name,
        items: lines.map(function (l) { return { item: l.item, qty: l.qty }; }),
        refund_method: $("r-method").value,
      }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) { rError.textContent = res.data.error || "تعذر تنفيذ المرتجع."; rError.hidden = false; return; }
        returnModal.hidden = true;
        $("d-title").textContent = "✓ تم تنفيذ المرتجع";
        $("d-name").textContent = res.data.name;
        $("d-change-label").textContent = num(res.data.refund_card) > 0 ? "مبلغ الرد للبطاقة: " : "مبلغ الرد نقداً للعميل: ";
        $("d-change").innerHTML = fmt(num(res.data.refund)) + config.currencyHtml;
        $("d-print").href = res.data.receipt_url + "?print=1";
        $("d-print").onclick = null;
        $("done-modal").hidden = false;
        $("d-new").focus();
        loadItems();
      })
      .catch(function () { rError.textContent = "خطأ في الاتصال بالخادم."; rError.hidden = false; })
      .finally(function () { busy = false; });
  });

  // اختصارات لوحة المفاتيح
  document.addEventListener("keydown", function (e) {
    if (e.key === "F2") { e.preventDefault(); search.focus(); search.select(); }
    else if (e.key === "F9") { e.preventDefault(); if (payModal.hidden) openPay(); }
    else if (e.key === "F8") { e.preventDefault(); openReturn(); }
    else if (e.key === "Escape") { payModal.hidden = true; returnModal.hidden = true; }
    else if (e.key === "Enter" && !payModal.hidden && document.activeElement !== $("m-confirm")) {
      e.preventDefault(); $("m-confirm").click();
    }
  });

  renderCart();
  loadItems();
  setStatus(navigator.onLine);
  renderPending();
  syncQueue(false);
})();
