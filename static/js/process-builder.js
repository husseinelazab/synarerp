(function () {
  "use strict";
  var button = document.getElementById("add-process-stage");
  if (!button) return;
  button.addEventListener("click", function () {
    var total = document.querySelector('[name="stages-TOTAL_FORMS"]');
    var index = Number(total.value);
    if (index >= 30) { button.disabled = true; return; }
    var html = document.getElementById("process-stage-template").innerHTML.replace(/__prefix__/g, String(index));
    document.getElementById("process-stages").insertAdjacentHTML("beforeend", html);
    total.value = index + 1;
    var position = document.querySelector('[name="stages-' + index + '-position"]');
    if (position) position.value = index + 1;
  });
})();
