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
})();
