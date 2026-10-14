/* موقع الجمعية العام: القائمة والقوائم المنسدلة، الشرائح، عدّاد الأثر، نسخ الآيبان، التبرع السريع */
(function () {
  "use strict";
  var body = document.body;

  // القائمة على الجوال
  var toggle = document.querySelector("[data-nav-toggle]");
  if (toggle) toggle.addEventListener("click", function () {
    var open = body.classList.toggle("nav-open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  // القوائم المنسدلة: بالنقر (الجوال ولوحة المفاتيح) وبالتمرير على الحاسب عبر CSS
  var items = document.querySelectorAll(".nav-item");
  items.forEach(function (item) {
    var btn = item.querySelector("button");
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = !item.classList.contains("open");
      items.forEach(function (other) { other.classList.remove("open"); other.querySelector("button").setAttribute("aria-expanded", "false"); });
      item.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });
  document.addEventListener("click", function () { items.forEach(function (i) { i.classList.remove("open"); }); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { items.forEach(function (i) { i.classList.remove("open"); }); body.classList.remove("nav-open"); }
  });

  // ظل الرأس وزر العودة للأعلى عند التمرير
  var onScroll = function () { body.classList.toggle("scrolled", window.scrollY > 40); };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // شرائح الواجهة
  document.querySelectorAll("[data-slider]").forEach(function (slider) {
    var slides = slider.querySelectorAll(".slide");
    var dots = slider.querySelectorAll(".slider-dots button");
    if (slides.length < 2) return;
    var index = 0, timer;
    var show = function (i) {
      index = (i + slides.length) % slides.length;
      slides.forEach(function (s, n) { s.classList.toggle("active", n === index); s.setAttribute("aria-hidden", n === index ? "false" : "true"); });
      dots.forEach(function (d, n) { d.classList.toggle("active", n === index); d.setAttribute("aria-current", n === index ? "true" : "false"); });
    };
    var play = function () {
      clearInterval(timer);
      if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) timer = setInterval(function () { show(index + 1); }, 6500);
    };
    dots.forEach(function (d, n) { d.addEventListener("click", function () { show(n); play(); }); });
    var prev = slider.querySelector("[data-prev]"), next = slider.querySelector("[data-next]");
    if (prev) prev.addEventListener("click", function () { show(index - 1); play(); });
    if (next) next.addEventListener("click", function () { show(index + 1); play(); });
    slider.addEventListener("mouseenter", function () { clearInterval(timer); });
    slider.addEventListener("mouseleave", play);
    show(0); play();
  });

  // عدّاد أرقام الأثر عند ظهوره
  var counters = document.querySelectorAll("[data-count]");
  var fmt = function (n) { return Math.round(n).toLocaleString("en-US"); };
  var run = function (el) {
    var target = parseInt(el.getAttribute("data-count"), 10) || 0, start = null;
    var step = function (t) {
      if (!start) start = t;
      var p = Math.min((t - start) / 1400, 1);
      el.textContent = fmt(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  if ("IntersectionObserver" in window && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { run(e.target); io.unobserve(e.target); } });
    }, { threshold: .4 });
    counters.forEach(function (el) { el.textContent = "0"; io.observe(el); });
  }

  // نسخ الآيبان
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy"), label = btn.querySelector("span");
      var done = function () {
        btn.classList.add("copied");
        if (label) label.textContent = "تم النسخ";
        setTimeout(function () { btn.classList.remove("copied"); if (label) label.textContent = "نسخ"; }, 1800);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
    });
  });

  // التبرع السريع: الفرصة المختارة تحدد وجهة النموذج، والمبلغ المكتوب يلغي المبالغ المقترحة
  document.querySelectorAll("[data-quick-donate]").forEach(function (form) {
    var select = form.querySelector("select[name=opportunity]");
    if (select) select.addEventListener("change", function () {
      form.action = select.options[select.selectedIndex].getAttribute("data-action");
    });
    var custom = form.querySelector("input[name=custom_amount]");
    if (custom) custom.addEventListener("input", function () {
      if (custom.value) form.querySelectorAll("input[name=amount]").forEach(function (r) { r.checked = false; });
    });
    form.querySelectorAll("input[name=amount]").forEach(function (r) {
      r.addEventListener("change", function () { if (custom) custom.value = ""; });
    });
  });
  // المتجر: المبلغ المكتوب يلغي المبالغ المقترحة في البطاقات وصفحة الفرصة
  document.querySelectorAll(".op-quick, [data-donate-form]").forEach(function (form) {
    var custom = form.querySelector("input[name=custom_amount]");
    if (!custom) return;
    custom.addEventListener("input", function () {
      if (custom.value) form.querySelectorAll("input[name=amount]").forEach(function (r) { r.checked = false; });
    });
    form.querySelectorAll("input[name=amount]").forEach(function (r) {
      r.addEventListener("change", function () { custom.value = ""; });
    });
  });

  // عداد الأسهم والكفالات مع الإجمالي
  document.querySelectorAll(".stepper").forEach(function (box) {
    var input = box.querySelector("input"), price = parseFloat(box.getAttribute("data-price")) || 0;
    var total = box.parentNode.querySelector("[data-total]");
    var sync = function () {
      var q = Math.max(1, Math.min(1000, parseInt(input.value, 10) || 1));
      if (total) total.textContent = fmt(q * price);
    };
    box.querySelectorAll("[data-step]").forEach(function (b) {
      b.addEventListener("click", function () {
        input.value = Math.max(1, Math.min(1000, (parseInt(input.value, 10) || 1) + parseInt(b.getAttribute("data-step"), 10)));
        sync();
      });
    });
    input.addEventListener("input", sync);
    sync();
  });

  if (document.querySelector(".sticky-donate")) body.classList.add("has-sticky");

  // حاسبة الزكاة
  document.querySelectorAll("[data-zakat]").forEach(function (root) {
    var num = function (el) { var v = parseFloat(el && el.value); return isFinite(v) && v > 0 ? v : 0; };
    var out = function (name) { return root.querySelector('[data-out="' + name + '"]'); };
    var priceInput = root.querySelector("[data-gold-price]"), pay = root.querySelector("[data-pay]");
    var calc = function () {
      var price = num(priceInput), total = 0;
      root.querySelectorAll("[data-add]").forEach(function (el) { total += num(el); });
      root.querySelectorAll("[data-gold]").forEach(function (el) {
        total += num(el) * parseInt(el.getAttribute("data-gold"), 10) / 24 * price;
      });
      root.querySelectorAll("[data-sub]").forEach(function (el) { total -= num(el); });
      total = Math.max(total, 0);
      var nisab = price ? 85 * price : 0, zakat = 0, msg;
      if (!total) msg = "أدخل أموالك لحساب الزكاة.";
      else if (!price) msg = "أدخل سعر جرام الذهب اليوم لمعرفة النصاب.";
      else if (total < nisab) msg = "لم تبلغ أموالك النصاب، فلا زكاة عليها.";
      else { zakat = Math.round(total * 0.025 * 100) / 100; msg = "بلغت أموالك النصاب، والزكاة الواجبة ربع العشر (2.5%)."; }
      out("total").textContent = fmt(total);
      out("nisab").textContent = nisab ? fmt(nisab) : "—";
      out("zakat").textContent = zakat.toLocaleString("en-US", { maximumFractionDigits: 2 });
      out("msg").textContent = msg;
      if (pay) {
        pay.href = pay.getAttribute("data-base") + (zakat ? "?amount=" + Math.ceil(zakat) + "#donate" : "");
        pay.classList.toggle("disabled", !zakat);
      }
    };
    root.addEventListener("input", calc);
    calc();
  });
})();
