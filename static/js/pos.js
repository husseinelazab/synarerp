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
        '<span class="product-price">' + fmt(i.rate) + '</span>' +
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
    $("t-grand").textContent = fmt(t.grand);
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
    $("m-grand").textContent = fmt(t.grand);
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
    out.textContent = fmt(Math.abs(change));
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
        $("d-name").textContent = res.data.name;
        $("d-change").textContent = fmt(num(res.data.change));
        $("d-print").href = res.data.receipt_url + "?print=1";
        $("done-modal").hidden = false;
        $("d-new").focus();
        loadItems();
      })
      .catch(function () { errEl.textContent = "خطأ في الاتصال بالخادم."; errEl.hidden = false; })
      .finally(function () { busy = false; });
  });

  $("d-new").addEventListener("click", function () { $("done-modal").hidden = true; resetSale(); });

  // اختصارات لوحة المفاتيح
  document.addEventListener("keydown", function (e) {
    if (e.key === "F2") { e.preventDefault(); search.focus(); search.select(); }
    else if (e.key === "F9") { e.preventDefault(); if (payModal.hidden) openPay(); }
    else if (e.key === "Escape") { payModal.hidden = true; }
    else if (e.key === "Enter" && !payModal.hidden && document.activeElement !== $("m-confirm")) {
      e.preventDefault(); $("m-confirm").click();
    }
  });

  renderCart();
  loadItems();
})();
