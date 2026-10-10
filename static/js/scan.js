/* أدوات مشتركة للماسح الضوئي: أصوات التأكيد، قراءة الكاميرا (إن دعمها المتصفح)، وتحليل «الكمية*الكود». */
(function () {
  "use strict";
  var audio = null;

  function beep(ok) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      var tones = ok ? [[1200, 0.07]] : [[260, 0.18], [200, 0.22]];
      var t = audio.currentTime;
      tones.forEach(function (tone) {
        var osc = audio.createOscillator(), gain = audio.createGain();
        osc.frequency.value = tone[0];
        osc.type = ok ? "sine" : "square";
        gain.gain.value = 0.12;
        osc.connect(gain); gain.connect(audio.destination);
        osc.start(t); osc.stop(t + tone[1]);
        t += tone[1] + 0.05;
      });
    } catch (e) { /* بدون صوت */ }
    if (!ok && navigator.vibrate) navigator.vibrate([120, 60, 120]);
  }

  // «5*628123» تعني 5 قطع من الصنف؛ وإلا فالكمية من الحقل
  function parse(text, defaultQty) {
    var m = /^\s*(\d+(?:\.\d+)?)\s*[*xX×]\s*(.+?)\s*$/.exec(text);
    if (m) return { qty: parseFloat(m[1]), code: m[2] };
    return { qty: defaultQty || 1, code: text.trim() };
  }

  // قراءة الباركود بكاميرا الجوال عبر BarcodeDetector (كروم أندرويد وسفاري/كروم ماك)
  function cameraSupported() { return "BarcodeDetector" in window && navigator.mediaDevices; }

  function openCamera(onCode) {
    var overlay = document.createElement("div");
    overlay.className = "cam-overlay";
    overlay.innerHTML = '<video playsinline muted></video><div class="cam-frame"></div>' +
      '<button type="button" class="btn cam-close">إغلاق الكاميرا</button>';
    document.body.appendChild(overlay);
    var video = overlay.querySelector("video"), stream = null, stopped = false, last = "", lastAt = 0;
    var detector = new window.BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39", "qr_code"] });
    function stop() {
      stopped = true;
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      overlay.remove();
    }
    overlay.querySelector(".cam-close").addEventListener("click", stop);
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (s) {
      stream = s; video.srcObject = s; video.play();
      (function loop() {
        if (stopped) return;
        detector.detect(video).then(function (codes) {
          var now = Date.now();
          // نفس الكود لا يُحسب مرتين خلال ثانيتين حتى لا تتكرر القراءة وهو أمام الكاميرا
          if (codes.length && (codes[0].rawValue !== last || now - lastAt > 2000)) {
            last = codes[0].rawValue; lastAt = now; onCode(last);
          }
        }).catch(function () {}).finally(function () { setTimeout(loop, 250); });
      })();
    }).catch(function () { alert("تعذر فتح الكاميرا. اسمح للمتصفح باستخدامها."); stop(); });
    return stop;
  }

  window.SynarScan = { beep: beep, parse: parse, cameraSupported: cameraSupported, openCamera: openCamera };
})();
