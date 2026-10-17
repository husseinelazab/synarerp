(function () {
  "use strict";

  var config = JSON.parse(document.getElementById("pos-config").textContent);
  var csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  var items = [], byId = {}, cart = [], group = "", busy = false, quoteData = null, quoteSeq = 0;

  var $ = function (id) { return document.getElementById(id); };
  var search = $("search"), grid = $("grid"), cartEl = $("cart"), discountEl = $("discount");

  function round2(n) { return Math.round((n + Number.EPSILON) * 100) / 100; }
  function round3(n) { return Math.round((n + Number.EPSILON) * 1000) / 1000; }
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
        buildCatalog();
        renderGrid();
      });
  }

  var catalog = [], visibleProducts = [], activeProduct = -1, productLimit = 60;
  function normalizeLookup(value) {
    return String(value || "").toLowerCase().replace(/[\u064b-\u065f\u0670\u0640]/g, "")
      .replace(/[أإآٱ]/g, "ا").replace(/[ة]/g, "ه").replace(/[ىئ]/g, "ي").replace(/ؤ/g, "و")
      .replace(/[٠-٩]/g, function (c) { return String(c.charCodeAt(0) - 1632); })
      .replace(/[۰-۹]/g, function (c) { return String(c.charCodeAt(0) - 1776); }).trim();
  }
  function scannerText(value) {
    return String(value || "").trim().replace(/[٠-٩]/g, function (c) { return String(c.charCodeAt(0) - 1632); })
      .replace(/[۰-۹]/g, function (c) { return String(c.charCodeAt(0) - 1776); });
  }
  function barcodeKey(value) { var code = scannerText(value); return /^0[0-9]{12}$/.test(code) && eanValid(code) ? code.slice(1) : code; }
  function buildCatalog() {
    catalog = [];
    items.forEach(function (item) {
      [null].concat(item.units || []).forEach(function (unit) {
        var name = item.name + (unit ? " (" + unit.name + ")" : "");
        var barcode = unit ? unit.barcode : item.barcode;
        catalog.push({item: item, unit: unit, name: name, barcode: barcode || item.code,
          text: normalizeLookup(name + " " + item.code + " " + (barcode || ""))});
      });
    });
  }
  function selectProduct(index) {
    activeProduct = index;
    grid.querySelectorAll(".product").forEach(function (row, i) {
      row.classList.toggle("selected", i === index);
    });
    var row = grid.querySelector('.product[data-index="' + index + '"]');
    if (row) row.scrollIntoView({block: "nearest"});
  }
  function renderGrid(keepLimit) {
    if (keepLimit !== true) productLimit = 60;
    grid.classList.add("product-list");
    var q = normalizeLookup(search.value), words = q.split(/\s+/).filter(Boolean);
    var list = catalog.filter(function (entry) {
      if (group && String(entry.item.group) !== group) return false;
      if (!q) return !entry.unit;
      return words.every(function (word) { return entry.text.indexOf(word) >= 0; });
    });
    if (q) list.sort(function (a, b) {
      function rank(entry) {
        if (barcodeKey(entry.barcode) === barcodeKey(search.value) || normalizeLookup(entry.item.code) === q) return 0;
        return normalizeLookup(entry.name).indexOf(q) === 0 ? 1 : 2;
      }
      return rank(a) - rank(b);
    });
    $("product-count").textContent = q ? list.length + " نتيجة مطابقة" : list.length + " منتج";
    visibleProducts = list.slice(0, productLimit);
    activeProduct = q && list.length ? 0 : -1;
    if (!list.length) { grid.innerHTML = '<div class="product-empty">لا توجد مطابقات. جرّب جزءاً آخر من الاسم أو الباركود.</div>'; return; }
    grid.innerHTML = visibleProducts.map(function (entry, index) {
      var i = entry.item, u = entry.unit;
      return '<button type="button" class="product' + (i.stock && i.qty <= 0 ? " out" : "") + (index === activeProduct ? " selected" : "") + '" data-id="' + i.id + '" data-index="' + index + '">' +
        '<span class="product-name">' + esc(entry.name) + '</span>' +
        '<span class="product-code" dir="ltr">' + esc(entry.barcode) + '</span>' +
        '<span class="product-price">' + fmt(u ? u.rate : i.rate) + config.currencyHtml + '</span>' +
        '<span class="product-qty">' + (i.stock ? round3(i.qty / (u ? u.factor : 1)) + " " + esc(u ? u.name : i.uom) : "خدمة") + '</span></button>';
    }).join("") + (list.length > productLimit ? '<button type="button" class="btn" id="product-more">عرض المزيد (' + (list.length-productLimit) + ')</button>' : "");
  }

  // unit: وحدة تحويل (كرتون…) {uom, name, factor, rate}؛ السطر يُميَّز بالصنف والوحدة
  function addToCart(id, qty, unit) {
    var item = byId[id];
    if (!item) return;
    var uom = unit ? unit.uom : null;
    var line = cart.find(function (l) { return l.id === item.id && (l.uom || null) === uom; });
    if (line) line.qty = round3(line.qty + (qty || 1));
    else cart.push({ id: item.id, name: unit ? item.name + " (" + unit.name + ")" : item.name,
                     rate: unit ? unit.rate : item.rate, qty: qty || 1, uom: uom, factor: unit ? unit.factor : 1 });
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
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (visibleProducts.length) selectProduct(Math.max(0, Math.min(visibleProducts.length - 1, activeProduct + (e.key === "ArrowDown" ? 1 : -1))));
      return;
    }
    if (e.key !== "Enter" || e.isComposing) return;
    e.preventDefault();
    var q = scannerText(search.value);
    if (!q) return;
    var match = items.find(function (i) { return i.barcode && barcodeKey(i.barcode) === barcodeKey(q); });
    if (!match) {
      // باركود وحدة تحويل (كرتون…)
      for (var k = 0; k < items.length && !match; k++) {
        var unit = (items[k].units || []).find(function (u) { return u.barcode && barcodeKey(u.barcode) === barcodeKey(q); });
        if (unit) { addToCart(items[k].id, 1, unit); search.value = ""; renderGrid(); return; }
      }
    }
    if (!match) match = items.find(function (i) { return i.code === q; });
    if (!match) {
      var codes = items.filter(function (i) { return normalizeLookup(i.code) === normalizeLookup(q); });
      if (codes.length === 1) match = codes[0];
    }
    if (!match) {
      var weighed = scaleBarcode(q);
      if (weighed) { addToCart(weighed.item.id, weighed.qty); search.value = ""; renderGrid(); return; }
    }
    if (!match) {
      var entry = visibleProducts[activeProduct];
      if (entry) { addToCart(entry.item.id, 1, entry.unit); search.value = ""; renderGrid(); return; }
    }
    if (match) { addToCart(match.id); search.value = ""; renderGrid(); }
  });

  // باركود الميزان: بادئة + رقم PLU + الوزن بالجرام (أو السعر بالهللة) + رقم تحقق
  function eanValid(code) {
    var sum = 0;
    for (var i = 0; i < 12; i++) sum += parseInt(code[i], 10) * (i % 2 ? 3 : 1);
    return (10 - sum % 10) % 10 === parseInt(code[12], 10);
  }
  function scaleBarcode(code) {
    var sc = config.scale;
    if (!sc || !sc.enabled || !/^\d{13}$/.test(code) || !eanValid(code)) return null;
    var prefix = sc.prefixes.find(function (p) { return code.indexOf(p) === 0; });
    if (!prefix) return null;
    var plu = parseInt(code.substr(prefix.length, sc.pluLength), 10);
    var value = parseInt(code.substring(prefix.length + sc.pluLength, 12), 10);
    var item = items.find(function (i) { return i.plu === plu; });
    if (!item || !value) return null;
    if (sc.valueType === "weight") return { item: item, qty: round3(value / 1000) };
    // الباركود يحمل السعر: الكمية = السعر ÷ سعر الكيلو شاملاً الضريبة
    var unit = config.pricesIncludeTax || item.zero_vat ? item.rate : item.rate * (100 + config.taxRate) / 100;
    return unit ? { item: item, qty: round3(value / 100 / unit) } : null;
  }

  $("groups").addEventListener("click", function (e) {
    var chip = e.target.closest(".chip");
    if (!chip) return;
    document.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
    chip.classList.add("active");
    group = chip.dataset.group;
    renderGrid();
  });

  grid.addEventListener("click", function (e) {
    if (e.target.closest("#product-more")) { productLimit += 60; renderGrid(true); return; }
    var card = e.target.closest(".product");
    if (card) {
      var entry = visibleProducts[Number(card.dataset.index)];
      if (entry) addToCart(entry.item.id, 1, entry.unit);
      search.focus(); search.select();
    }
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
    quoteData = null;
    $("m-coupon").value = "";
    $("m-points").value = "";
    if (discountEl) discountEl.value = 0;
    $("customer").value = config.defaultCustomer;
    renderCart();
    search.focus();
  }

  // نافذة الدفع
  var payModal = $("pay-modal"), cashEl = $("m-cash"), cardEl = $("m-card"), errEl = $("m-error");

  // المطلوب فعلاً: من تسعير الخادم (العروض والكوبون والنقاط) إن توفر، وإلا من حساب السلة
  function due() { return quoteData ? quoteData.due : totals().grand; }

  function cartPayload() {
    return cart.map(function (l) { return { item: l.id, qty: l.qty, rate: l.rate, uom: l.uom || null }; });
  }

  function renderQuote() {
    var box = $("m-promos"), pts = $("m-points-info");
    $("m-grand").innerHTML = fmt(due()) + config.currencyHtml;
    if (!quoteData) { box.hidden = true; pts.textContent = ""; return; }
    var lines = quoteData.lines.filter(function (l) { return l.rule; }).map(function (l) {
      return "<div>" + (l.free ? "🎁 " : "🏷️ ") + esc(l.name) + " — " + esc(l.rule) +
        (l.free ? " (هدية × " + l.qty + ")" : " (" + fmt(l.price_list_rate) + " ← " + fmt(l.rate) + ")") + "</div>";
    });
    var saved = round2(quoteData.lines.reduce(function (s, l) { return s + (l.price_list_rate - l.rate) * l.qty; }, 0));
    if (saved > 0) lines.push('<div class="ok">وفّر العميل ' + fmt(saved) + config.currencyHtml + "</div>");
    if (quoteData.loyalty_amount) lines.push("<div>نقاط الولاء: −" + fmt(quoteData.loyalty_amount) + config.currencyHtml + "</div>");
    if ($("m-coupon").value.trim() && !quoteData.coupon_ok) lines.push('<div class="text-danger">الكوبون غير صالح لهذه السلة.</div>');
    box.innerHTML = lines.join("");
    box.hidden = !lines.length;
    pts.textContent = quoteData.points === null ? "العميل غير مسجل في برنامج ولاء" :
      "رصيد النقاط: " + quoteData.points + " (النقطة = " + quoteData.point_value + ")";
    $("m-points").disabled = quoteData.points === null;
  }

  function requestQuote() {
    if (!config.quoteUrl || !navigator.onLine) { quoteData = null; renderQuote(); return Promise.resolve(); }
    var seq = ++quoteSeq;
    return fetch(config.quoteUrl, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ customer: $("customer").value, discount: totals().discount, items: cartPayload(),
                             coupon: $("m-coupon").value.trim(), loyalty_points: num($("m-points").value) }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (seq !== quoteSeq) return;
        if (!res.ok) { quoteData = null; renderQuote(); $("m-promos").hidden = false; $("m-promos").innerHTML = '<div class="text-danger">' + esc(res.data.error) + "</div>"; return; }
        quoteData = res.data;
        renderQuote();
        cashEl.value = $("m-credit").checked ? 0 : due().toFixed(2);
        cardEl.value = 0;
        updateChange();
      })
      .catch(function () { if (seq === quoteSeq) { quoteData = null; renderQuote(); } });
  }

  function openPay() {
    if (!cart.length) return;
    var t = totals();
    if (config.allowDiscount && t.discount > t.total * config.maxDiscountPercent / 100 + 0.001) {
      alert("الخصم يتجاوز الحد المسموح (" + config.maxDiscountPercent + "%).");
      return;
    }
    $("m-credit").checked = false;
    quoteData = null;
    renderQuote();
    requestQuote();
    cashEl.value = t.grand.toFixed(2);
    cardEl.value = 0;
    errEl.hidden = true;
    $("m-auth").value = ""; $("m-last4").value = "";
    terminalMsg("");
    payModal.hidden = false;
    updateChange();
    cashEl.select();
  }

  function updateChange() {
    var change = round2(num(cashEl.value) + num(cardEl.value) - due());
    var label = $("m-change-label"), out = $("m-change");
    label.textContent = change < 0 ? "المتبقي على العميل: " : "الباقي للعميل: ";
    out.innerHTML = fmt(Math.abs(change)) + config.currencyHtml;
    out.classList.toggle("neg", change < 0);
    var credit = $("m-credit").checked;
    var registered = $("customer").value && Number($("customer").value) !== Number(config.defaultCustomer);
    $("m-confirm").disabled = busy || (credit ? (!registered || !navigator.onLine) : change < 0);
    $("m-card-ref").hidden = !(cardMode() === "manual" && num(cardEl.value) > 0);
  }

  $("m-credit").addEventListener("change", function () {
    if (this.checked) { cashEl.value = 0; cardEl.value = 0; }
    updateChange();
  });
  $("customer").addEventListener("change", updateChange);
  cashEl.addEventListener("input", updateChange);
  cardEl.addEventListener("input", updateChange);
  $("pay").addEventListener("click", openPay);
  $("m-cancel").addEventListener("click", function () { payModal.hidden = true; quoteData = null; });
  var quoteTimer = null;
  ["m-coupon", "m-points"].forEach(function (id) {
    $(id).addEventListener("input", function () { clearTimeout(quoteTimer); quoteTimer = setTimeout(requestQuote, 400); });
  });
  document.querySelector(".quick-pay").addEventListener("click", function (e) {
    var q = e.target.dataset.quick;
    if (!q) return;
    var grand = due();
    if (q === "exact") { cashEl.value = grand.toFixed(2); cardEl.value = 0; }
    else if (q === "card") { cardEl.value = grand.toFixed(2); cashEl.value = 0; }
    else { cashEl.value = num(q).toFixed(2); }
    updateChange();
  });

  // ---------- أجهزة الدفع (مدى) ----------
  // الاتصال بجهاز مدى محلي بالكامل: من متصفح الكاشير إلى البرنامج الوسيط على نفس الجهاز.
  // خادم النظام لا يتصل بأي جهة دفع؛ يستلم فقط نتيجة العملية (رقم الموافقة والمرجع وآخر 4 أرقام).
  function cardMode() { return config.terminalMode || "manual"; }

  function terminalMsg(text, kind) {
    var el = $("m-terminal");
    el.hidden = !text;
    el.textContent = text || "";
    el.className = "terminal-status" + (kind ? " " + kind : "");
  }

  function bridgeCall(path, payload, timeoutMs) {
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, timeoutMs || 120000) : null;
    return fetch(config.bridgeUrl + path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      signal: ctrl ? ctrl.signal : undefined, credentials: "omit", cache: "no-store",
    })
      .then(function (r) { return r.json(); })
      .catch(function (e) {
        throw new Error(e && e.name === "AbortError" ? "انتهت مهلة انتظار جهاز الدفع." :
          "تعذر الاتصال ببرنامج جهاز الدفع على هذا الجهاز. تأكد من تشغيله.");
      })
      .finally(function () { if (timer) clearTimeout(timer); });
  }

  function simulate(kind, amount) {
    // وضع التدريب: يوافق بعد لحظة، ويرفض المبالغ المنتهية بـ .51 لتجربة حالة الرفض
    return new Promise(function (resolve) {
      setTimeout(function () {
        if (Math.round(amount * 100) % 100 === 51) { resolve({ status: "declined", message: "رُفضت (محاكاة)" }); return; }
        var n = String(Date.now());
        resolve({ status: "approved", amount: amount.toFixed(2), auth_code: "SIM" + n.slice(-3), rrn: n.slice(-12), scheme: "mada",
                  masked_pan: "588848******" + n.slice(-4), terminal_id: config.terminalId || "SIMULATOR" });
      }, 1200);
    });
  }

  // يطلب من جهاز الدفع تنفيذ العملية ويعيد بيانات الموافقة (أو null إن لم يكن هناك مبلغ بطاقة)
  function obtainCard(kind, amount, reference, original) {
    var mode = cardMode();
    if (amount <= 0) return Promise.resolve(null);
    if (mode === "manual") {
      var auth = (kind === "sale" ? $("m-auth") : $("r-auth")).value.trim();
      if (kind === "sale" && config.requireCardRef && !auth) {
        return Promise.reject(new Error("أدخل رقم الموافقة المطبوع على إيصال جهاز مدى."));
      }
      return Promise.resolve({ mode: mode, auth_code: auth, card_last4: kind === "sale" ? $("m-last4").value.trim() : "" });
    }
    var req = mode === "simulator" ? simulate(kind, amount) : bridgeCall("/" + kind, {
      amount: amount.toFixed(2), currency: "SAR", reference: reference,
      original_rrn: original && original.rrn || "", original_auth_code: original && original.auth_code || "",
    });
    return req.then(function (res) {
      if (!res || res.status !== "approved") throw new Error(res && res.message ? "رفض جهاز الدفع: " + res.message : "لم يوافق جهاز الدفع على العملية.");
      res.mode = mode;
      if (!res.terminal_id) res.terminal_id = config.terminalId;
      return res;
    });
  }

  // إلغاء عملية بطاقة وافق عليها الجهاز ثم رفضها النظام، حتى لا يُخصم من العميل دون فاتورة
  function voidCard(card) {
    if (!card || card.mode !== "bridge") return Promise.resolve(true);
    return bridgeCall("/void", { rrn: card.rrn || "", auth_code: card.auth_code || "" }, 60000)
      .then(function (r) { return r && r.status === "voided"; }, function () { return false; });
  }

  function showSaleError(message) { errEl.textContent = message; errEl.hidden = false; }

  $("m-confirm").addEventListener("click", function () {
    if (busy) return;
    if ($("m-credit").checked && (!navigator.onLine || Number($("customer").value) === Number(config.defaultCustomer))) {
      showSaleError("البيع الآجل يتطلب اتصالاً واختيار عميل مسجل غير العميل النقدي."); return;
    }
    busy = true;
    errEl.hidden = true;
    var t = totals(), cardAmount = num(cardEl.value), clientId = newId();
    if (cardAmount > 0 && cardMode() !== "manual") terminalMsg("⏳ أدخل البطاقة أو قرّبها من جهاز مدى… المبلغ " + fmt(cardAmount), "waiting");
    $("m-confirm").disabled = true;
    obtainCard("sale", cardAmount, clientId)
      .then(function (card) {
        if (card && card.mode !== "manual") terminalMsg("✓ تمت الموافقة " + (card.auth_code || card.rrn), "ok");
        if (!navigator.onLine) { saveOffline(t, card, clientId); return; }
        return submitSale(t, card, clientId);
      }, function (err) { terminalMsg(""); showSaleError(err.message); })
      .finally(function () { busy = false; $("m-confirm").disabled = false; updateChange(); });
  });

  function submitSale(t, card, clientId) {
    return fetch(config.checkoutUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({
        customer: $("customer").value,
        discount: t.discount,
        credit_sale: $("m-credit").checked,
        paid_cash: num(cashEl.value),
        paid_card: num(cardEl.value),
        card: card,
        coupon: $("m-coupon").value.trim(),
        loyalty_points: num($("m-points").value),
        items: cartPayload(),
      }),
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok) {
          var msg = res.data.error || "تعذر إتمام البيع.";
          if (card && card.mode === "bridge") {
            return voidCard(card).then(function (voided) {
              showSaleError(msg + (voided ? " — أُلغيت عملية البطاقة على الجهاز."
                : " — تنبيه: وافق جهاز مدى على العملية ولم يُتمكن من إلغائها آلياً؛ ألغها من الجهاز يدوياً (المرجع " + (card.rrn || card.auth_code) + ")."));
            });
          }
          showSaleError(msg);
          return;
        }
        payModal.hidden = true;
        $("d-title").textContent = "✓ تمت عملية البيع";
        $("d-change-label").textContent = num(res.data.outstanding) > 0 ? "المتبقي على حساب العميل: " : "الباقي للعميل: ";
        $("d-name").textContent = res.data.name;
        $("d-change").innerHTML = fmt(num(res.data.outstanding) > 0 ? num(res.data.outstanding) : num(res.data.change)) + config.currencyHtml;
        $("d-print").href = res.data.receipt_url + "?print=1";
        $("d-print").onclick = null;
        $("done-modal").hidden = false;
        $("d-new").focus();
        loadItems();
      })
      .catch(function () { saveOffline(t, card, clientId); });  // انقطع الاتصال أثناء الإرسال: يُحفظ البيع في الجهاز
  }

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

  function saveOffline(t, card, clientId) {
    if ($("m-credit").checked) {
      showSaleError("تعذر تأكيد البيع الآجل بسبب الاتصال. راجع آخر فواتير العميل قبل إعادة المحاولة؛ لم يُحفظ بيع آجل دون اتصال.");
      return;
    }
    var sale = {
      client_id: clientId || newId(), card: card || null, created_at: new Date().toISOString(), customer: $("customer").value,
      customer_name: $("customer").selectedOptions[0] ? $("customer").selectedOptions[0].text : "",
      discount: t.discount, paid_cash: num(cashEl.value), paid_card: num(cardEl.value),
      items: cart.map(function (l) { return { item: l.id, qty: l.qty, rate: l.rate, name: l.name, uom: l.uom || null }; }),
      totals: t, status: "pending", error: "",
    };
    var q = readQueue();
    q.push(sale);
    writeQueue(q);
    // خصم الكميات محلياً حتى يظهر الرصيد التقريبي
    cart.forEach(function (l) { var i = byId[l.id]; if (i && i.stock) i.qty = round3(i.qty - l.qty * (l.factor || 1)); });
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
  var returnModal = $("return-modal"), rError = $("r-error"), rInvoice = null, rRefund = 0;
  var returnSearchSeq = 0, invoiceLoadSeq = 0, returnSearchTimer;

  function openReturn() {
    if (busy) return;
    if (!navigator.onLine) { alert("المرتجعات تحتاج اتصالاً بالخادم للتحقق من الفاتورة الأصلية."); return; }
    returnModal.hidden = false;
    $("r-body").hidden = true;
    $("r-confirm").disabled = true;
    rError.hidden = true;
    $("r-invoice").value = "";
    $("r-date").value = "";
    resetReturnSelection();
    searchReturnInvoices();
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
    rRefund = round2(Math.max(0, total * (rInvoice ? rInvoice.factor : 0) - num(rInvoice && rInvoice.outstanding)));
    $("r-total").textContent = fmt(rRefund);
    $("r-confirm").disabled = !(valid && any);
  }

  function resetReturnSelection() {
    invoiceLoadSeq++;
    rInvoice = null; rRefund = 0;
    $("r-items").innerHTML = "";
    $("r-body").hidden = true;
    $("r-confirm").disabled = true;
    $("r-matches").hidden = false;
  }
  function searchReturnInvoices(autoOpen) {
    if (busy) return;
    clearTimeout(returnSearchTimer);
    resetReturnSelection();
    var seq = ++returnSearchSeq, query = $("r-invoice").value.trim();
    rError.hidden = true;
    $("r-matches").textContent = "جارٍ البحث عن الفواتير…";
    fetch(config.invoiceSearchUrl + "?q=" + encodeURIComponent(query) + "&date=" + encodeURIComponent($("r-date").value), {credentials: "same-origin"})
      .then(function (r) { return r.json().then(function (data) { if (!r.ok) throw new Error(data.error || "تعذر البحث"); return data; }); })
      .then(function (data) {
        if (seq !== returnSearchSeq) return;
        var list = data.invoices;
        if (autoOpen && query) {
          var exact = list.find(function (i) { return normalizeLookup(i.name) === normalizeLookup(query); });
          var choice = exact || (list.length === 1 && !data.has_more ? list[0] : null);
          if (choice && !choice.fully_returned) { loadInvoice(choice.name); return; }
        }
        $("r-matches").innerHTML = '<p class="muted">' + (query ? "مطابقات البحث" : "آخر فواتير البيع") +
          (data.has_more ? " · أول 30 فاتورة؛ ضيّق البحث أو اختر التاريخ" : "") + '</p>' +
          (list.length ? list.map(function (i) {
            return '<button type="button" class="return-invoice" data-invoice="' + esc(i.name) + '"' + (i.fully_returned ? " disabled" : "") + '>' +
              '<strong dir="ltr">' + esc(i.name) + '</strong><span>' + esc(i.customer) + '</span>' +
              '<small>' + esc(i.date) + ' · ' + esc(new Date(i.time).toLocaleTimeString("ar-SA", {hour:"2-digit", minute:"2-digit"})) +
              (i.phone ? ' · ' + esc(i.phone) : '') + '</small><b>' + fmt(num(i.grand_total)) + config.currencyHtml +
              '</b><small>' + (i.fully_returned ? "مُرجعة بالكامل" : "اختيار للإرجاع") + '</small></button>';
          }).join("") : '<p class="product-empty">لا توجد فواتير مطابقة. جرّب رقم العميل أو جزءاً من رقم الفاتورة أو تاريخ البيع.</p>');
      }).catch(function (error) {
        if (seq !== returnSearchSeq) return;
        $("r-matches").textContent = "تعذر تحميل الفواتير.";
        rError.textContent = error.message || "تحقق من الاتصال وأعد البحث."; rError.hidden = false;
      });
  }

  function loadInvoice(name) {
    resetReturnSelection();
    var seq = ++invoiceLoadSeq;
    if (!name) return;
    rError.hidden = true;
    fetch(config.invoiceUrl + "?name=" + encodeURIComponent(name), { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (seq !== invoiceLoadSeq) return;
        if (!res.ok) { rError.textContent = res.data.error; rError.hidden = false; $("r-body").hidden = true; return; }
        rInvoice = res.data;
        $("r-matches").hidden = true;
        $("r-info").textContent = "العميل: " + rInvoice.customer + " · التاريخ: " + rInvoice.date + " · الإجمالي: " + fmt(num(rInvoice.grand_total));
        $("r-items").innerHTML = rInvoice.items.map(function (i) {
          return "<tr><td>" + esc(i.name) + "</td><td>" + i.sold + "</td><td>" + i.returnable + "</td><td>" + fmt(i.rate) +
            '</td><td><input type="number" class="form-control" min="0" step="any" max="' + i.returnable + '" value="0" data-item="' + i.item + '" data-rate="' + i.rate + '"></td></tr>';
        }).join("");
        $("r-method").value = num(rInvoice.paid_card) > 0 ? "card" : "cash";
        $("r-auth").value = "";
        toggleReturnRef();
        $("r-body").hidden = false;
        updateReturnTotal();
        var first = $("r-items").querySelector("input");
        if (first) { first.focus(); first.select(); }
      }).catch(function () {
        if (seq !== invoiceLoadSeq) return;
        rError.textContent = "تعذر تحميل تفاصيل الفاتورة. أعد اختيارها."; rError.hidden = false;
      });
  }

  $("open-return").addEventListener("click", openReturn);
  $("r-cancel").addEventListener("click", function () { returnModal.hidden = true; search.focus(); });
  $("r-load").addEventListener("click", function () { searchReturnInvoices(true); });
  $("r-recent").addEventListener("click", function () { $("r-invoice").value = ""; $("r-date").value = ""; searchReturnInvoices(); });
  $("r-date").addEventListener("change", function () { searchReturnInvoices(); });
  $("r-change").addEventListener("click", function () { searchReturnInvoices(); $("r-invoice").focus(); });
  $("r-invoice").addEventListener("input", function () {
    returnSearchSeq++; resetReturnSelection();
    clearTimeout(returnSearchTimer);
    $("r-matches").textContent = "جارٍ البحث…";
    returnSearchTimer = setTimeout(searchReturnInvoices, 220);
  });
  $("r-matches").addEventListener("click", function (e) {
    var button = e.target.closest("[data-invoice]");
    if (button && !button.disabled) { returnSearchSeq++; loadInvoice(button.dataset.invoice); }
  });
  $("r-invoice").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); searchReturnInvoices(true); } });
  $("r-items").addEventListener("input", updateReturnTotal);
  function toggleReturnRef() { $("r-card-ref").hidden = !($("r-method").value === "card" && cardMode() === "manual"); }
  $("r-method").addEventListener("change", toggleReturnRef);

  $("r-confirm").addEventListener("click", function () {
    if (busy || !rInvoice) return;
    busy = true;
    var invoiceForReturn = rInvoice;
    var lockedControls = Array.from(returnModal.querySelectorAll("input,select,button")).map(function (el) {
      var state = {el: el, disabled: el.disabled}; el.disabled = true; return state;
    });
    rError.hidden = true;
    var lines = returnLines().filter(function (l) { return l.qty > 0; });
    var method = $("r-method").value;
    // مبلغ الرد للبطاقة يُحسب نهائياً في الخادم؛ هنا التقدير المعروض نفسه لإرساله لجهاز مدى
    var refund = method === "card" ? rRefund : 0;
    obtainCard("refund", refund, newId(), invoiceForReturn.card)
      .then(function (card) {
        return fetch(config.returnUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify({
            invoice: invoiceForReturn.name,
            items: lines.map(function (l) { return { item: l.item, qty: l.qty }; }),
            refund_method: method,
            card: card,
          }),
        })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
          .then(function (res) {
            if (!res.ok) {
              rError.textContent = (res.data.error || "تعذر تنفيذ المرتجع.") +
                (card && card.mode === "bridge" ? " — تنبيه: نُفذ الاسترداد على جهاز مدى (المرجع " + (card.rrn || card.auth_code) + ")؛ راجعه." : "");
              rError.hidden = false;
              return;
            }
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
          }, function () { rError.textContent = "خطأ في الاتصال بالخادم."; rError.hidden = false; });
      }, function (err) { rError.textContent = err.message; rError.hidden = false; })
      .finally(function () {
        busy = false; lockedControls.forEach(function (state) { state.el.disabled = state.disabled; });
        updateReturnTotal();
      });
  });

  // اختصارات لوحة المفاتيح
  document.addEventListener("keydown", function (e) {
    if (busy && ["F8", "F9", "Escape"].includes(e.key)) { e.preventDefault(); return; }
    if (e.key === "F2") { e.preventDefault(); var target = returnModal.hidden ? search : $("r-invoice"); target.focus(); target.select(); }
    else if (e.key === "F9") { e.preventDefault(); if (payModal.hidden && returnModal.hidden) openPay(); }
    else if (e.key === "F8") { e.preventDefault(); if (payModal.hidden) openReturn(); }
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
