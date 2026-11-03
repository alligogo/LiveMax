// LiveMax 대시보드 — 최소한의 바닐라 JS (외부 라이브러리 없음)
(function () {
  "use strict";

  // ---- 모니터링 화면 자동 갱신 (/api/status) ----
  const mon = document.getElementById("monitor");
  if (mon) {
    const sec = parseInt(mon.dataset.refresh || "10", 10);
    const get = (o, path) => path.split(".").reduce((a, k) => (a == null ? a : a[k]), o);
    const fmt = (v, f) => {
      if (v == null) return "-";
      if (f === "pct") return Number(v).toFixed(1) + "%";
      if (f === "mbps") return Number(v).toFixed(1) + " Mbps";
      return String(v);
    };
    const tick = async () => {
      if (document.hidden) return;
      try {
        const r = await fetch("/api/status", { credentials: "same-origin" });
        if (r.status === 401) { location.href = "/login"; return; }
        const d = await r.json();
        mon.querySelectorAll("[data-k]").forEach((el) => { el.textContent = fmt(get(d, el.dataset.k), el.dataset.fmt); });
        (d.lives || []).forEach((lv) => {
          const li = mon.querySelector(`[data-live="${CSS.escape(lv.id)}"]`);
          if (!li) return;
          li.className = "live s-" + lv.state;
          li.querySelector(".ic").textContent = lv.icon;
          li.querySelectorAll("[data-f]").forEach((el) => { el.textContent = lv[el.dataset.f] ?? "-"; });
        });
      } catch (e) { /* 네트워크 일시 오류는 무시하고 다음 주기에 재시도 */ }
    };
    setInterval(tick, sec * 1000);
  }

  // ---- 드래그앤드롭 업로드 (진행률 표시, 다중 파일, 순차 전송) ----
  const drop = document.getElementById("drop");
  if (drop) {
    const list = drop.querySelector(".uploads");
    const input = drop.querySelector("input[type=file]");
    const exts = drop.dataset.accept.split(",");
    const maxBytes = parseInt(drop.dataset.maxMb, 10) * 1024 * 1024;
    const queue = [];
    let busy = false;

    const add = (files) => {
      for (const f of files) {
        const li = document.createElement("li");
        const ok = exts.some((e) => f.name.toLowerCase().endsWith(e));
        li.innerHTML = `<span></span><progress max="100" value="0"></progress>`;
        li.firstChild.textContent = f.name + (ok ? "" : " — 허용되지 않는 확장자");
        list.appendChild(li);
        if (!ok || f.size > maxBytes) { li.querySelector("progress").remove(); if (ok) li.firstChild.textContent += " — 용량 초과"; continue; }
        queue.push({ f, li });
      }
      next();
    };
    const next = () => {
      if (busy || !queue.length) return;
      busy = true;
      const { f, li } = queue.shift();
      const bar = li.querySelector("progress");
      const xhr = new XMLHttpRequest();
      const url = `/media/${drop.dataset.kind}/upload/${encodeURIComponent(drop.dataset.folder)}?name=${encodeURIComponent(f.name)}`;
      xhr.open("PUT", url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = (e.loaded / e.total) * 100; };
      xhr.onload = () => {
        let msg = "";
        try { const j = JSON.parse(xhr.responseText); msg = j.message || j.error || ""; } catch (e) { msg = xhr.statusText; }
        li.firstChild.textContent = `${f.name} — ${xhr.status === 200 ? "✓ " : "✕ "}${msg}`;
        bar.remove(); busy = false; next();
        if (!queue.length && !busy) setTimeout(() => location.reload(), 20000); // 검증 후 목록 갱신
      };
      xhr.onerror = () => { li.firstChild.textContent = `${f.name} — ✕ 전송 실패`; bar.remove(); busy = false; next(); };
      xhr.send(f);
    };
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => add(e.dataTransfer.files));
    input.addEventListener("change", () => add(input.files));
  }

  // ---- 편성 구간 추가 ----
  const addseg = document.getElementById("addseg");
  if (addseg) {
    addseg.addEventListener("click", () => {
      const box = document.getElementById("segments");
      const i = box.querySelectorAll(".seg").length;
      const html = document.getElementById("segtpl").innerHTML.replaceAll("__I__", String(i));
      box.insertAdjacentHTML("beforeend", html);
    });
  }
})();
