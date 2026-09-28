/* 送餐一屏核对 + 进程内提醒轮询(纯原生 JS,无依赖) */
(function () {
  "use strict";

  // ---- 通用 toast ----
  function toast(text, kind) {
    var wrap = document.getElementById("toast-wrap");
    if (!wrap) return;
    var el = document.createElement("div");
    el.className = "toast";
    if (kind === "err") el.style.borderLeftColor = "#8c1d18";
    if (kind === "ok") el.style.borderLeftColor = "#1f8f82";
    el.textContent = text;
    wrap.appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }

  // 页面跳转携带的 flash(msg/kind)
  var qs = new URLSearchParams(location.search);
  if (qs.get("msg")) toast(qs.get("msg"), qs.get("kind"));

  // ---- 提醒铃铛 ----
  var bellDot = document.getElementById("bell-dot");
  var pendingReminders = 0;
  function refreshBell() {
    if (!bellDot) return;
    if (pendingReminders > 0) {
      bellDot.style.display = "flex";
      bellDot.textContent = pendingReminders > 9 ? "9+" : pendingReminders;
    } else {
      bellDot.style.display = "none";
    }
  }
  var bell = document.getElementById("bell");
  if (bell) {
    bell.addEventListener("click", function () {
      if (pendingReminders === 0) { toast("暂无送餐提醒"); return; }
      pendingReminders = 0; refreshBell();
    });
  }
  function pollReminders() {
    fetch("/api/reminders").then(function (r) { return r.json(); }).then(function (items) {
      if (items && items.length) {
        pendingReminders += items.length;
        items.forEach(function (it) {
          toast("🔔 送餐提醒:" + it.title + (it.route_code ? "（" + it.route_code + "）" : ""), "ok");
        });
        refreshBell();
      }
    }).catch(function () {});
  }
  if (bellDot) { setInterval(pollReminders, 4000); pollReminders(); }

  // ---- 扫码核对页 ----
  var input = document.getElementById("scan-input");
  if (!input || typeof window.__ROUTE__ === "undefined") return;

  var ROUTE = window.__ROUTE__;
  var activeStop = null;       // 住户扫描成功后建立的当前停靠点
  var resultBox = document.getElementById("scan-result");
  var stage1 = document.getElementById("stage1");
  var stage2 = document.getElementById("stage2");

  function setStage(which) {
    stage1.classList.remove("active", "done");
    stage2.classList.remove("active", "done");
    if (which === 1) stage1.classList.add("active");
    if (which === 2) { stage1.classList.add("done"); stage2.classList.add("active"); }
    if (which === 0) { stage1.classList.add("done"); stage2.classList.add("done"); }
  }
  setStage(1);

  function showResult(kind, msg) {
    resultBox.className = "scan-result " + (kind === "ok" ? "ok"
      : kind === "dup" ? "dup" : "err");
    resultBox.textContent = (kind === "ok" ? "✅ " : kind === "dup" ? "⚠️ " : "⛔ ") + msg;
  }

  function sendScan(payload) {
    payload = (payload || "").trim();
    if (!payload) return;
    var url = "/api/scan?route=" + encodeURIComponent(ROUTE)
      + "&q=" + encodeURIComponent(payload)
      + (activeStop ? "&stop=" + encodeURIComponent(activeStop) : "");
    fetch(url).then(function (r) { return r.json(); }).then(function (res) {
      if (res.accepted && res.stage === "resident_ok") {
        activeStop = res.stop_id;
        setStage(2);
        showResult("ok", res.message);
        toast(res.message, "ok");
        input.value = ""; input.focus();
      } else if (res.accepted && res.stage === "delivered") {
        showResult("ok", res.message);
        toast(res.message, "ok");
        setStage(0);
        activeStop = null;
        setTimeout(function () { location.reload(); }, 900);
      } else if (res.stage === "duplicate") {
        showResult("dup", res.message);
        toast(res.message, "err");
        input.select();
      } else {
        // error:旧签作废 / 错餐隔离 / 人盒不符 / 顺序错误 / 权限等
        showResult("err", res.message);
        toast(res.message, "err");
        input.select();
      }
    }).catch(function () {
      showResult("err", "网络错误,请重试");
    });
  }

  document.getElementById("scan-send").addEventListener("click", function () {
    sendScan(input.value);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); sendScan(input.value); }
  });

  // 无扫码枪时:点击二维码 = 本地适配器模拟一次扫描
  document.addEventListener("click", function (e) {
    var chip = e.target.closest(".qr-chip");
    if (!chip) return;
    e.preventDefault();
    var payload = chip.getAttribute("data-scan");
    input.value = payload;
    sendScan(payload);
  });
})();
