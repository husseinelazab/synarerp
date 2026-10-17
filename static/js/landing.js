(function () {
  'use strict';
  var groups = Array.from(document.querySelectorAll('[data-plan-category]'));
  var controls = document.querySelector('[data-pricing-controls]');
  if (!controls || !groups.length) return;
  var categoryButtons = Array.from(document.querySelectorAll('[data-category]'));
  function chooseCategory(kind) {
    if (!groups.some(function (g) { return g.dataset.planCategory === kind; })) return;
    groups.forEach(function (g) { g.hidden = g.dataset.planCategory !== kind; });
    categoryButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.category === kind)); });
  }
  categoryButtons.forEach(function (b) { b.addEventListener('click', function () { chooseCategory(b.dataset.category); }); });
  document.querySelectorAll('[data-choose-category]').forEach(function (a) { a.addEventListener('click', function () { chooseCategory(a.dataset.chooseCategory); }); });
  var periodButtons = Array.from(document.querySelectorAll('[data-period]'));
  periodButtons.forEach(function (b) { b.addEventListener('click', function () {
    var period = b.dataset.period;
    document.querySelectorAll('[data-price-period]').forEach(function (price) { price.hidden = price.dataset.pricePeriod !== period; });
    periodButtons.forEach(function (button) { button.setAttribute('aria-pressed', String(button === b)); });
  }); });
  var initial = groups.find(function (g) { return !!g.querySelector('.plan-card'); }) || groups[0];
  chooseCategory(initial.dataset.planCategory);
  controls.hidden = false;
}());
