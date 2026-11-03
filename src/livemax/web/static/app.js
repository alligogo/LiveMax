// LiveMax v2 웹 UI — 프레임워크 없는 바닐라 JS. 사용자 데이터는 항상 textContent 로 넣는다(HTML 주입 방지).
// 서버는 시각을 epoch 초로 주고, 여기서 브라우저 현지 시간으로 표시한다.
"use strict";

// ------------------------------------------------------------------ 도우미
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v; // 고정 문자열(아이콘)에만 사용
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (s) => document.querySelector(s);
const pad = (n) => String(n).padStart(2, "0");
const fmt = {
  time(ts) { if (!ts) return "-"; const d = new Date(ts * 1000); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; },
  clock(ts) { if (!ts) return "-"; const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; },
  dur(sec) {
    if (sec == null || sec < 0) return "-";
    sec = Math.floor(sec); const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d) return `${d}일 ${hh}시간`; if (hh) return `${hh}:${pad(m)}:${pad(s)}`; return `${m}:${pad(s)}`;
  },
  bytes(b) { if (b == null) return "-"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return `${b.toFixed(i ? 1 : 0)} ${u[i]}`; },
  num(v, d = 1, unit = "") { return v == null ? "-" : `${Number(v).toFixed(d)}${unit}`; },
};
const STATUS = { ok: "정상", warn: "경고", down: "중단", restarting: "재시작중", stopped: "정지됨" };
const MODES = [["random_norepeat", "중복방지 랜덤"], ["random", "완전 랜덤"], ["sequential_loop", "순차 반복"], ["sequential", "순차 1회"]];
const ICON = {
  edit: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  out: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>',
  trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14"/></svg>',
};

function toast(msg, isErr) {
  const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  $("#toast-root").append(t);
  setTimeout(() => t.remove(), isErr ? 7000 : 3500);
}

async function api(method, url, body, raw) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body instanceof Blob || body instanceof ArrayBuffer) { opt.body = body; }
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (raw) return r;
  let j = {};
  try { j = await r.json(); } catch (e) { /* 비어 있는 응답 */ }
  if (r.status === 401 && url !== "/api/login") { showLogin(); throw new Error(j.error || "로그인이 필요합니다"); }
  if (r.status === 428) { showChangePassword(true); throw new Error(j.error); }
  if (!r.ok && r.status !== 409) throw new Error(j.error || `오류 (${r.status})`);
  j._status = r.status;
  return j;
}

// ------------------------------------------------------------------ 레이아웃
let pollTimer = null;
function stopPoll() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

function shell(active, ...content) {
  const nav = [["#/", "라이브", "lives"], ["#/media/video", "영상", "video"], ["#/media/audio", "음원", "audio"], ["#/system", "설정", "system"]];
  return h("div", {},
    h("header", { class: "topbar" },
      h("a", { class: "brand", href: "#/" }, h("span", { class: "dot" }), "LiveMax"),
      h("nav", { class: "nav" }, nav.map(([href, label, key]) => h("a", { href, class: active === key ? "on" : "" }, label))),
      h("button", { class: "iconbtn", title: "로그아웃", html: ICON.out, onclick: logout })),
    h("main", { class: "page" }, content));
}

function mount(node) { $("#app").replaceChildren(node); }

async function logout() { try { await api("POST", "/api/logout"); } catch (e) { /* 무시 */ } showLogin(); }

// ------------------------------------------------------------------ 로그인 / 비밀번호
function showLogin() {
  stopPoll(); closeModal();
  const user = h("input", { class: "input", value: "admin", autocomplete: "username" });
  const pw = h("input", { class: "input", type: "password", autocomplete: "current-password", autofocus: true });
  const msg = h("div", { class: "msg err" });
  const submit = async (e) => {
    e.preventDefault(); msg.textContent = "";
    try {
      const r = await api("POST", "/api/login", { username: user.value, password: pw.value });
      if (r.must_change) showChangePassword(true); else route();
    } catch (err) { msg.textContent = err.message; }
  };
  mount(h("div", { class: "login-wrap" }, h("form", { class: "login", onsubmit: submit },
    h("div", { class: "brand" }, h("span", { class: "dot" }), "LiveMax"),
    h("div", { class: "card" }, h("div", { class: "card-head" }, "로그인"), h("div", { class: "card-body" },
      h("label", { class: "field" }, h("span", {}, "아이디"), user),
      h("label", { class: "field" }, h("span", {}, "비밀번호"), pw),
      msg, h("button", { class: "btn primary", style: "width:100%;margin-top:6px" }, "로그인"),
      h("p", { class: "hint" }, "5회 실패하면 15분간 차단됩니다. 분실 시 서버에서 'livemax password'"))))));
  setTimeout(() => pw.focus(), 50);
}

function showChangePassword(forced) {
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password" });
  const n1 = h("input", { class: "input", type: "password", autocomplete: "new-password" });
  const n2 = h("input", { class: "input", type: "password", autocomplete: "new-password" });
  const msg = h("div", { class: "msg err" });
  openModal(forced ? "초기 비밀번호 변경" : "비밀번호 변경", [
    forced ? h("p", { class: "muted" }, "설치 때 만들어진 임시 비밀번호입니다. 계속하려면 새 비밀번호로 바꿔 주세요.") : null,
    h("label", { class: "field" }, h("span", {}, "현재 비밀번호"), cur),
    h("label", { class: "field" }, h("span", {}, "새 비밀번호 (12자 이상)"), n1),
    h("label", { class: "field" }, h("span", {}, "새 비밀번호 확인"), n2), msg,
  ], [
    forced ? null : h("button", { class: "btn ghost", onclick: closeModal }, "취소"),
    h("span", { class: "spacer" }),
    h("button", { class: "btn primary", onclick: async () => {
      msg.textContent = "";
      if (n1.value !== n2.value) { msg.textContent = "새 비밀번호가 서로 다릅니다"; return; }
      try { await api("POST", "/api/password", { current: cur.value, new: n1.value }); closeModal(); toast("비밀번호를 변경했습니다"); route(); }
      catch (e) { msg.textContent = e.message; }
    } }, "변경"),
  ], { locked: forced });
}

// ------------------------------------------------------------------ 모달
function openModal(title, body, foot, opts = {}) {
  closeModal();
  const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back && !opts.locked) closeModal(); } },
    h("div", { class: "modal", style: opts.wide ? "width:min(1000px,100%)" : "" },
      h("div", { class: "card-head" }, title, h("span", { class: "spacer" }),
        opts.locked ? null : h("button", { class: "iconbtn", style: "width:28px;height:28px;background:transparent", onclick: closeModal }, "✕")),
      h("div", { class: "modal-body" }, body),
      foot ? h("div", { class: "modal-foot" }, foot) : null));
  $("#modal-root").replaceChildren(back);
}
function closeModal() { $("#modal-root").replaceChildren(); }

// ------------------------------------------------------------------ 라이브 (메인)
let lastOverview = null;

async function showLives() {
  stopPoll();
  const grid = h("div", { class: "grid" });
  const side = h("aside", { class: "side" });
  mount(shell("lives", h("div", { class: "layout" }, h("section", {}, grid), side)));
  const refresh = async () => {
    try { lastOverview = await api("GET", "/api/overview"); } catch (e) { return; }
    renderLives(grid, lastOverview); renderSide(side, lastOverview);
  };
  await refresh();
  pollTimer = setInterval(refresh, 5000);
}

function renderLives(grid, ov) {
  const now = ov.server_time;
  const cards = ov.lives.map((lv) => {
    const running = lv.state === "running";
    const preview = h("div", { class: "preview" },
      lv.clip && lv.clip_folder
        ? h("img", { src: `/api/thumbs/${encodeURIComponent(lv.clip_folder)}/${encodeURIComponent(lv.clip)}`, alt: "", loading: "lazy" })
        : h("div", { class: "ph" }, running ? "미리보기 준비 중" : "송출 중이 아닙니다"),
      running ? h("span", { class: "badge-live" }, "LIVE") : null);
    const tile = (val, label, cls) => h("div", { class: "tile " + (cls || "") }, h("b", {}, val), h("span", {}, label));
    const speedCls = lv.speed == null ? "" : lv.speed >= 0.97 ? "good" : "bad";
    return h("article", { class: "card live" },
      h("div", { class: "card-head" }, h("span", { class: "status " + lv.status }, STATUS[lv.status] || lv.status),
        h("span", { style: "text-transform:none;letter-spacing:0;font-size:14px;font-weight:600;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, lv.name),
        h("span", { class: "spacer" }),
        h("button", { class: "iconbtn", style: "width:30px;height:30px;background:transparent", title: "설정", html: ICON.edit, onclick: () => openLiveEditor(lv) })),
      preview,
      h("div", { class: "card-body" },
        h("div", { class: "tiles" },
          tile(lv.started_at ? fmt.dur(now - lv.started_at) : "-", "가동 시간"),
          tile(lv.bitrate_kbps ? Math.round(lv.bitrate_kbps).toLocaleString() : "-", "kbit/s"),
          tile(lv.speed != null ? lv.speed.toFixed(2) + "x" : "-", "speed", speedCls)),
        h("div", { class: "nowline" }, h("span", { class: "lbl" }, "♪"), h("span", { class: "val" }, lv.song_title ? `${lv.song_title}${lv.song_artist ? " — " + lv.song_artist : ""}` : "-")),
        h("div", { class: "nowline" }, h("span", { class: "lbl" }, "▶"), h("span", { class: "val" }, lv.clip ? `${lv.clip_folder} / ${lv.clip}` : "-")),
        lv.message ? h("div", { class: "msg" + (lv.status === "down" ? " err" : "") }, lv.message) : null,
        h("div", { class: "actions" },
          lv.enabled
            ? h("button", { class: "btn danger", onclick: () => liveAction(lv, "stop") }, "정지")
            : h("button", { class: "btn primary", onclick: () => liveAction(lv, "start") }, "시작"),
          h("button", { class: "btn", disabled: !lv.enabled, onclick: () => liveAction(lv, "restart") }, "재시작")),
        h("div", { class: "links" },
          lv.youtube_video_id ? h("a", { onclick: () => openPreview(lv) }, "유튜브 미리보기") : null,
          h("a", { onclick: () => openLog(lv) }, "로그"),
          h("span", { class: "faint" }, `다음 자동 재시작 ${fmt.time(lv.next_restart)}`))));
  });
  cards.push(h("button", { class: "add-card", onclick: () => openLiveEditor(null) }, h("div", {}, h("span", { class: "plus" }, "+"), "라이브 추가")));
  grid.replaceChildren(...cards);
}

function renderSide(side, ov) {
  const s = ov.system || {}, c = ov.capacity || {};
  const counts = { ok: 0, warn: 0, down: 0 };
  ov.lives.forEach((l) => { if (counts[l.status] !== undefined) counts[l.status]++; });
  const ratio = Math.min(1, c.usage_ratio || 0);
  const kv = (k, v) => h("div", { class: "kv" }, h("span", { class: "muted" }, k), h("b", {}, v));
  side.replaceChildren(
    h("div", { class: "card" }, h("div", { class: "card-head" }, "용량"),
      h("div", { class: "card-body" },
        h("div", { class: "big" }, h("b", {}, c.can_add ?? "-"), h("span", {}, "개 더 추가 가능 (안전 기준)")),
        h("div", { class: "meter" }, h("i", { class: ratio > 0.85 ? "hi" : ratio > 0.7 ? "mid" : "", style: `width:${(ratio * 100).toFixed(0)}%` })),
        h("div", { class: "small muted", style: "text-align:center" }, `월 예상 ${fmt.num(c.projected_tb, 1)} / ${fmt.num(c.limit_tb, 0)} TB`))),
    h("div", { class: "card", style: "margin-top:16px" }, h("div", { class: "card-head" }, "시스템"),
      h("div", { class: "card-body" },
        h("div", { class: "group" }, "라이브"),
        kv("정상 / 경고 / 중단", `${counts.ok} / ${counts.warn} / ${counts.down}`),
        h("div", { class: "group" }, "서버"),
        kv("CPU", fmt.num(s.cpu_pct, 1, "%")), kv("steal time", fmt.num(s.steal_pct, 2, "%")),
        kv("메모리", s.mem_total_mb ? `${Math.round(s.mem_used_mb)} / ${Math.round(s.mem_total_mb)} MB` : "-"),
        kv("디스크", s.disk_total_gb ? `${fmt.num(s.disk_used_gb, 1)} / ${fmt.num(s.disk_total_gb, 0)} GB` : "-"),
        h("div", { class: "group" }, "트래픽"),
        kv("현재 송신", fmt.num(s.net_tx_mbps, 1, " Mbps")),
        kv("이번 달 누적", s.month_tx_bytes != null ? fmt.bytes(s.month_tx_bytes) : "-"),
        kv("라이브당", `${fmt.num(c.per_live_mbps, 1)} Mbps`))),
    h("div", { class: "card", style: "margin-top:16px" }, h("div", { class: "card-head" }, "알림"),
      h("div", { class: "card-body" }, ov.alerts.length
        ? h("ul", { class: "alerts" }, ov.alerts.slice(0, 12).map((a) => h("li", { class: a.level }, h("span", { class: "ic" }),
            h("span", {}, h("time", {}, fmt.time(a.ts)), a.live ? h("b", {}, liveName(a.live) + " ") : null, a.message))))
        : h("div", { class: "muted small" }, "알림 없음"))));
}

function liveName(id) { const l = (lastOverview?.lives || []).find((x) => x.id === id); return l ? l.name : id; }

async function liveAction(lv, action) {
  const label = { start: "시작", stop: "정지", restart: "재시작" }[action];
  if (action !== "start" && !confirm(`'${lv.name}' 을(를) ${label}할까요?\n${action === "stop" ? "유튜브 송출이 멈춥니다." : "몇 초 끊겼다가 같은 방송으로 돌아옵니다."}`)) return;
  try { await api("POST", `/api/lives/${lv.id}/${action}`); toast(`${lv.name}: ${label} 요청`); setTimeout(route, 800); }
  catch (e) { toast(e.message, true); }
}

function openPreview(lv) {
  openModal(`미리보기 — ${lv.name}`, h("div", { style: "aspect-ratio:16/9" },
    h("iframe", { src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(lv.youtube_video_id)}?autoplay=1&mute=1`,
      style: "width:100%;height:100%;border:0;border-radius:8px", allow: "autoplay; encrypted-media", allowfullscreen: true })), null, { wide: true });
}

async function openLog(lv) {
  const sel = h("div", { class: "seg" });
  const pre = h("pre", { class: "log" }, "불러오는 중…");
  const load = async (name) => {
    [...sel.children].forEach((b) => b.classList.toggle("on", b.dataset.n === name));
    try { const r = await api("GET", `/api/logs?name=${name}&lines=300`); pre.textContent = r.text; pre.scrollTop = pre.scrollHeight; }
    catch (e) { pre.textContent = e.message; }
  };
  [["live", "송출"], ["audio", "음원"], ["tls", "TLS 중계"]].forEach(([p, label]) =>
    sel.append(h("button", { "data-n": `${p}-${lv.id}`, onclick: () => load(`${p}-${lv.id}`) }, label)));
  openModal(`로그 — ${lv.name}`, [sel, h("div", { style: "height:12px" }), pre], null, { wide: true });
  load(`live-${lv.id}`);
}

// ------------------------------------------------------------------ 라이브 추가/수정
async function openLiveEditor(lv) {
  const isNew = !lv;
  let media;
  try { media = { video: await api("GET", "/api/media/video"), audio: await api("GET", "/api/media/audio") }; }
  catch (e) { toast(e.message, true); return; }
  const cur = lv || { name: "", video_folders: [], audio_folders: [], audio_mode: "random_norepeat", norepeat_count: 30, crossfade: 0, bitrate: "5500k", youtube_video_id: "" };
  const name = h("input", { class: "input", value: cur.name, placeholder: "예: 재즈 색소폰 24/7", maxlength: 80 });
  const key = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: lv ? `현재 키 ${lv.key_masked || "없음"} — 바꿀 때만 입력` : "유튜브 스튜디오의 스트림 키" });
  const checks = (kind, selected) => {
    const box = h("div", { class: "checks" });
    if (!media[kind].folders.length) box.append(h("div", { class: "muted small" }, `${kind === "video" ? "영상" : "음원"} 폴더가 없습니다. 먼저 '${kind === "video" ? "영상" : "음원"}' 메뉴에서 폴더를 만들고 파일을 올리세요.`));
    media[kind].folders.forEach((f) => {
      const cb = h("input", { type: "checkbox", value: f.name });
      cb.checked = selected.includes(f.name);
      const lab = h("label", { class: "check" + (cb.checked ? " on" : "") }, cb, h("span", { class: "nm" }, f.name), h("span", { class: "ct" }, `${f.count}`));
      cb.addEventListener("change", () => lab.classList.toggle("on", cb.checked));
      box.append(lab);
    });
    box.values = () => [...box.querySelectorAll("input:checked")].map((x) => x.value);
    return box;
  };
  const vbox = checks("video", cur.video_folders), abox = checks("audio", cur.audio_folders);
  let mode = cur.audio_mode;
  const modeSeg = h("div", { class: "seg" }, MODES.map(([v, l]) => {
    const b = h("button", { type: "button", class: v === mode ? "on" : "", onclick: () => { mode = v; [...modeSeg.children].forEach((x) => x.classList.toggle("on", x === b)); } }, l);
    return b;
  }));
  const bitrate = h("input", { class: "input", value: cur.bitrate });
  const norep = h("input", { class: "input", type: "number", min: 0, max: 1000, value: cur.norepeat_count });
  const xfade = h("input", { class: "input", type: "number", min: 0, max: 10, step: 0.5, value: cur.crossfade });
  const vid = h("input", { class: "input", value: cur.youtube_video_id || "", placeholder: "선택 — youtu.be/ 뒤의 영상 ID" });
  const msg = h("div", { class: "msg err" });
  const adv = h("details", {}, h("summary", { class: "muted", style: "cursor:pointer;margin:6px 0 14px" }, "고급 설정"),
    h("label", { class: "field" }, h("span", {}, "비트레이트"), bitrate, h("div", { class: "hint" }, "copy 송출이라 실제 비트레이트는 영상 파일 자체의 값입니다. 이 값은 용량 계산에 쓰입니다.")),
    h("label", { class: "field" }, h("span", {}, "중복방지 곡 수"), norep),
    h("label", { class: "field" }, h("span", {}, "크로스페이드 (초)"), xfade, h("div", { class: "hint" }, "0 = 오디오 copy (CPU 최소). 0보다 크면 이 라이브만 오디오를 디코딩·재인코딩합니다.")),
    h("label", { class: "field" }, h("span", {}, "유튜브 영상 ID"), vid, h("div", { class: "hint" }, "넣으면 카드에서 유튜브 미리보기를 열 수 있습니다.")));

  const payload = (apply) => ({ name: name.value, stream_key: key.value, video_folders: vbox.values(), audio_folders: abox.values(),
    audio_mode: mode, bitrate: bitrate.value, norepeat_count: Number(norep.value), crossfade: Number(xfade.value), youtube_video_id: vid.value, apply });
  const save = async (apply) => {
    msg.textContent = "";
    try {
      if (isNew) await api("POST", "/api/lives", payload(apply));
      else await api("PUT", `/api/lives/${lv.id}`, payload(apply));
      closeModal(); toast(apply === "now" ? (isNew ? "저장하고 송출을 시작했습니다" : "저장하고 적용했습니다 (몇 초 끊겼다가 복귀)") : "저장했습니다");
      route();
    } catch (e) { msg.textContent = e.message; }
  };
  openModal(isNew ? "라이브 추가" : `라이브 설정 — ${lv.name}`, [
    h("label", { class: "field" }, h("span", {}, "이름"), name),
    h("label", { class: "field" }, h("span", {}, "스트림 키"), key, h("div", { class: "hint" }, "유튜브 스튜디오 → 라이브 스트리밍 → 스트림 키. 저장 후에는 마지막 4자리만 보입니다.")),
    h("div", { class: "field" }, h("span", { class: "muted small", style: "display:block;margin-bottom:8px;font-weight:600" }, "영상 폴더 (여러 개 선택 가능)"), vbox),
    h("div", { class: "field", style: "margin-top:14px" }, h("span", { class: "muted small", style: "display:block;margin-bottom:8px;font-weight:600" }, "음원 폴더 (여러 개 선택 가능)"), abox),
    h("div", { class: "field", style: "margin-top:14px" }, h("span", { class: "muted small", style: "display:block;margin-bottom:8px;font-weight:600" }, "재생 방식"), modeSeg),
    adv, msg,
  ], [
    isNew ? null : h("button", { class: "btn danger", onclick: async () => {
      if (!confirm(`'${lv.name}' 라이브를 삭제할까요? 송출이 멈추고 설정과 스트림 키가 지워집니다.`)) return;
      try { await api("DELETE", `/api/lives/${lv.id}`); closeModal(); toast("삭제했습니다"); route(); } catch (e) { msg.textContent = e.message; }
    } }, "삭제"),
    h("span", { class: "spacer" }),
    h("button", { class: "btn ghost", onclick: closeModal }, "취소"),
    h("button", { class: "btn", onclick: () => save("later") }, isNew ? "저장만" : "저장 (다음 재시작 때 적용)"),
    h("button", { class: "btn primary", onclick: () => save("now") }, isNew ? "저장하고 시작" : "저장하고 지금 적용"),
  ], { wide: true });
  setTimeout(() => name.focus(), 50);
}

// ------------------------------------------------------------------ 미디어
let mediaFolder = { video: null, audio: null };

async function showMedia(kind) {
  stopPoll();
  const label = kind === "video" ? "영상" : "음원";
  const left = h("div", { class: "card" });
  const right = h("div", {});
  mount(shell(kind, h("div", { class: "media-layout" }, left, right)));
  let data;
  const load = async () => {
    data = await api("GET", `/api/media/${kind}`);
    if (!mediaFolder[kind] || !data.folders.find((f) => f.name === mediaFolder[kind])) mediaFolder[kind] = data.folders[0]?.name || null;
    const newName = h("input", { class: "input", placeholder: "새 폴더 이름", maxlength: 64 });
    left.replaceChildren(
      h("div", { class: "card-head" }, `${label} 폴더`),
      h("ul", { class: "folder-list" }, data.folders.length ? data.folders.map((f) =>
        h("li", { class: f.name === mediaFolder[kind] ? "on" : "", onclick: () => { mediaFolder[kind] = f.name; load(); } },
          h("span", { class: "nm" }, f.name), f.pending ? h("span", { class: "pill y" }, `검증 ${f.pending}`) : null, h("span", { class: "ct" }, f.count)))
        : h("li", { class: "muted" }, "폴더가 없습니다")),
      h("div", { class: "card-body", style: "border-top:1px solid var(--line)" },
        h("form", { class: "row", onsubmit: async (e) => { e.preventDefault(); try { await api("POST", `/api/media/${kind}`, { name: newName.value }); mediaFolder[kind] = newName.value.trim(); load(); } catch (er) { toast(er.message, true); } } },
          newName, h("button", { class: "btn primary sm" }, "만들기")),
        h("p", { class: "hint" }, kind === "video"
          ? "규격: H.264 High 1920x1080 30fps 5000k 이상, 키프레임 2초, B프레임·오디오 없음. PC의 변환기(LiveMax-Convert)로 만들면 됩니다."
          : "규격: AAC-LC 128k 44.1kHz 스테레오 (-14 LUFS). PC의 변환기(LiveMax-Convert)로 만들면 됩니다.")));
    await renderFolder(kind, right, data);
  };
  await load();
  pollTimer = setInterval(async () => { if (!uploading) { try { await load(); } catch (e) { /* 무시 */ } } }, 10000);
}

let uploading = 0;

async function renderFolder(kind, root, data) {
  const folder = mediaFolder[kind];
  if (!folder) { root.replaceChildren(h("div", { class: "card" }, h("div", { class: "card-body muted" }, "왼쪽에서 폴더를 만드세요."))); return; }
  const info = data.folders.find((f) => f.name === folder);
  const detail = await api("GET", `/api/media/${kind}/${encodeURIComponent(folder)}`);
  const input = h("input", { type: "file", multiple: true, hidden: true, accept: kind === "video" ? ".mp4,.mkv" : ".m4a,.aac" });
  const list = h("div", { class: "uploads" });
  const drop = h("div", { class: "drop" }, h("div", {}, h("b", {}, "파일을 여기로 끌어다 놓거나 "), h("a", { style: "color:var(--green);cursor:pointer", onclick: () => input.click() }, "선택"), input),
    h("div", { class: "small faint", style: "margin-top:6px" }, "큰 파일도 끊기면 이어서 올라갑니다 · 올린 뒤 자동으로 규격 검증"));
  const go = (files) => uploadFiles(kind, folder, files, list);
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); go(e.dataTransfer.files); });
  input.addEventListener("change", () => go(input.files));

  const files = kind === "video"
    ? h("div", { class: "files" }, detail.files.map((f) => h("div", { class: "file" },
        h("div", { class: "th" }, f.thumb ? h("img", { loading: "lazy", alt: "", src: `/api/thumbs/${encodeURIComponent(folder)}/${encodeURIComponent(f.name)}` }) : "🎞"),
        h("div", { class: "meta" }, h("div", { class: "nm", title: f.name }, f.name),
          h("div", { class: "faint" }, `${fmt.dur(f.duration)} · ${fmt.bytes(f.size)}`, f.status === "delete_pending" ? " · 삭제 예약" : "")),
        h("button", { class: "btn sm danger del", html: ICON.trash, title: "삭제", onclick: () => delFile(kind, folder, f.name) }))))
    : h("table", { class: "list-table" }, h("tbody", {}, detail.files.map((f) => h("tr", {},
        h("td", {}, f.name), h("td", { class: "r" }, fmt.dur(f.duration)), h("td", { class: "r" }, fmt.bytes(f.size)),
        h("td", { class: "r", style: "width:40px" }, h("button", { class: "btn sm danger", html: ICON.trash, title: "삭제", onclick: () => delFile(kind, folder, f.name) }))))));

  root.replaceChildren(
    h("div", { class: "card" },
      h("div", { class: "card-head" }, folder, h("span", { class: "spacer" }),
        info?.used_by?.length ? h("span", { class: "pill g", style: "text-transform:none" }, `사용 중: ${info.used_by.map(liveName).join(", ")}`) : null,
        h("button", { class: "btn sm ghost", onclick: () => renameFolder(kind, folder) }, "이름 변경"),
        h("button", { class: "btn sm danger", onclick: () => deleteFolder(kind, folder) }, "삭제")),
      h("div", { class: "card-body" }, drop, list,
        detail.pending.length ? h("div", { class: "small", style: "margin-top:12px" }, h("span", { class: "pill y" }, "규격 검증 중"), " ", detail.pending.join(", ")) : null,
        h("div", { class: "small muted", style: "margin-top:14px" }, `${detail.files.length}개 · ${fmt.bytes(info?.size || 0)}`),
        detail.files.length ? files : h("div", { class: "muted small", style: "margin-top:10px" }, "아직 파일이 없습니다."))),
    data.rejected.length ? h("div", { class: "card", style: "margin-top:20px" }, h("div", { class: "card-head" }, "검증 실패 (규격 불일치)"),
      h("div", { class: "card-body" }, h("ul", { class: "alerts" }, data.rejected.slice(0, 30).map((r) =>
        h("li", { class: "warn" }, h("span", { class: "ic" }), h("span", {}, h("time", {}, fmt.time(r.ts)), h("b", {}, `${r.folder}/${r.name} `), r.reasons.join(" · "))))))) : null);
}

async function delFile(kind, folder, name) {
  if (!confirm(`'${name}' 을(를) 삭제할까요?`)) return;
  try { const r = await api("DELETE", `/api/media/${kind}/${encodeURIComponent(folder)}/${encodeURIComponent(name)}`); toast(r.message || "삭제했습니다"); showMedia(kind); }
  catch (e) { toast(e.message, true); }
}
async function renameFolder(kind, folder) {
  const n = prompt("새 폴더 이름", folder); if (!n || n === folder) return;
  try { await api("PUT", `/api/media/${kind}/${encodeURIComponent(folder)}`, { name: n }); mediaFolder[kind] = n.trim(); showMedia(kind); } catch (e) { toast(e.message, true); }
}
async function deleteFolder(kind, folder) {
  if (!confirm(`'${folder}' 폴더를 삭제할까요? (비어 있어야 합니다)`)) return;
  try { await api("DELETE", `/api/media/${kind}/${encodeURIComponent(folder)}`); mediaFolder[kind] = null; showMedia(kind); } catch (e) { toast(e.message, true); }
}

// 이어 올리기: 8MB 조각, 끊기면 서버가 가진 지점부터 다시. 동시에 3개 파일.
const CHUNK = 8 * 1024 * 1024;
async function uploadFiles(kind, folder, fileList, listEl) {
  const ext = kind === "video" ? [".mp4", ".mkv"] : [".m4a", ".aac"];
  const queue = [...fileList].filter((f) => {
    if (ext.some((e) => f.name.toLowerCase().endsWith(e))) return true;
    toast(`${f.name}: ${ext.join(", ")} 파일만 올릴 수 있습니다`, true); return false;
  });
  const one = async (file) => {
    const bar = h("i", { style: "width:0%" });
    const txt = h("span", {}, "준비 중");
    const row = h("div", { class: "up" }, h("div", { class: "row" }, h("span", { style: "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, file.name), txt), h("div", { class: "bar" }, bar));
    listEl.prepend(row);
    uploading++;
    try {
      let { id, offset } = await api("POST", "/api/uploads", { kind, folder, name: file.name, size: file.size });
      let fails = 0; const t0 = Date.now(); const start = offset;
      while (offset < file.size) {
        try {
          const r = await api("PUT", `/api/uploads/${id}?offset=${offset}`, file.slice(offset, offset + CHUNK));
          offset = r.offset; fails = 0;
          const pct = (offset / file.size) * 100, sec = (Date.now() - t0) / 1000;
          bar.style.width = pct.toFixed(1) + "%";
          txt.textContent = `${pct.toFixed(0)}% · ${fmt.bytes((offset - start) / Math.max(sec, 0.1))}/s`;
        } catch (e) {
          if (++fails > 8) throw e;
          txt.textContent = `연결 끊김 — ${fails}회째 다시 시도`;
          await new Promise((r) => setTimeout(r, Math.min(30000, 1000 * 2 ** fails)));
          const again = await api("POST", "/api/uploads", { kind, folder, name: file.name, size: file.size });
          offset = again.offset;
        }
      }
      await api("POST", `/api/uploads/${id}/complete`);
      bar.style.width = "100%"; txt.textContent = "완료 · 규격 검증 중";
    } catch (e) { row.classList.add("err"); txt.textContent = "실패: " + e.message; }
    finally { uploading--; }
  };
  const workers = Array.from({ length: Math.min(3, queue.length) }, async () => { while (queue.length) await one(queue.shift()); });
  await Promise.all(workers);
  setTimeout(() => showMedia(kind), 4000);
}

// ------------------------------------------------------------------ 설정 / 시스템
async function showSystem() {
  stopPoll();
  let s;
  try { s = await api("GET", "/api/system"); } catch (e) { return; }
  const [bh, bm] = s.settings.restart_base_utc.split(":").map(Number);
  const local = new Date(Date.UTC(2000, 0, 1, bh, bm));
  const base = h("input", { class: "input", type: "time", value: `${pad(local.getHours())}:${pad(local.getMinutes())}`, style: "max-width:160px" });
  const relay = h("input", { type: "checkbox" }); relay.checked = s.settings.tls_relay;
  const logSel = h("select", { class: "input", style: "max-width:260px" }, h("option", { value: "livemaxd" }, "관리 프로그램 (livemaxd)"),
    (lastOverview?.lives || []).flatMap((l) => [["live", "송출"], ["audio", "음원"], ["tls", "TLS"]].map(([p, n]) => h("option", { value: `${p}-${l.id}` }, `${l.name} — ${n}`))));
  const pre = h("pre", { class: "log", style: "max-height:40vh" }, "");
  const card = (title, ...body) => h("div", { class: "card", style: "margin-bottom:20px" }, h("div", { class: "card-head" }, title), h("div", { class: "card-body" }, body));
  const imp = h("input", { type: "file", accept: ".zip", hidden: true });
  imp.addEventListener("change", async () => {
    if (!imp.files[0]) return;
    try { const r = await api("POST", "/api/system/import", imp.files[0]); toast(r.message); } catch (e) { toast(e.message, true); }
  });
  mount(shell("system", h("div", { class: "grid", style: "grid-template-columns:repeat(auto-fill,minmax(420px,1fr))" },
    h("div", {},
      card("업데이트",
        h("p", {}, "현재 버전: ", h("b", { class: "mono" }, s.version)),
        s.update_requested ? h("p", { class: "msg" }, "업데이트 요청됨 — 잠시 후 자동으로 새 버전으로 바뀝니다.") : null,
        h("button", { class: "btn primary", onclick: async () => {
          if (!confirm("새 버전으로 업데이트할까요? 모든 라이브가 잠깐 끊겼다가 차례대로 복귀합니다.")) return;
          try { const r = await api("POST", "/api/system/update"); toast(r.message); } catch (e) { toast(e.message, true); }
        } }, "최신 버전으로 업데이트")),
      s.reboot_required ? card("서버 재부팅 필요",
        h("p", {}, "보안 업데이트(커널 등)가 설치되어 재부팅을 기다리고 있습니다. 송출 영향이 적은 시간에 서버에서 ", h("code", {}, "sudo reboot"), " 하세요."),
        s.reboot_packages.length ? h("p", { class: "small muted" }, s.reboot_packages.join(", ")) : null) : null,
      card("송출 설정",
        h("label", { class: "field" }, h("span", {}, "일일 자동 재시작 시작 시각 (내 시간 기준)"), base,
          h("div", { class: "hint" }, "라이브마다 이 시각부터 2분 간격으로 하루 1번 재시작합니다 (몇 초 끊김). 시청자가 적은 시간으로 정하세요.")),
        h("label", { class: "check", style: "margin-bottom:14px" }, relay, h("span", { class: "nm" }, "TLS 중계 사용 (송출 트래픽 약 25% 절감)")),
        h("button", { class: "btn primary", onclick: async () => {
          const [lh, lm] = base.value.split(":").map(Number); const d = new Date(2000, 0, 1, lh, lm);
          try { await api("PUT", "/api/system/settings", { restart_base_utc: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`, tls_relay: relay.checked }); toast("저장했습니다 (각 라이브의 다음 재시작 때 적용)"); }
          catch (e) { toast(e.message, true); }
        } }, "저장")),
      card("디스크",
        h("table", { class: "list-table", style: "margin-top:0" }, h("tbody", {}, s.disk.map((d) => h("tr", {}, h("td", {}, d.label), h("td", { class: "r" }, fmt.bytes(d.bytes)))))),
        h("button", { class: "btn", style: "margin-top:12px", onclick: async () => { const r = await api("POST", "/api/system/cleanup"); toast(r.message); } }, "지금 정리 실행"),
        h("p", { class: "hint" }, "임시 파일·지난 목록·오래된 로그만 지웁니다. 올린 영상·음원 원본은 건드리지 않습니다.")),
      card("설정 백업",
        h("p", { class: "small muted" }, "라이브 설정을 내려받아 다른 서버에 그대로 옮길 수 있습니다. 스트림 키는 포함되지 않습니다."),
        h("div", { class: "row" }, h("a", { class: "btn", href: "/api/system/export" }, "내보내기"), h("button", { class: "btn", onclick: () => imp.click() }, "가져오기"), imp))),
    h("div", {},
      card("보안",
        h("button", { class: "btn", onclick: () => showChangePassword(false) }, "비밀번호 변경"),
        h("div", { class: "section-title", style: "margin-top:18px" }, "로그인된 기기"),
        h("table", { class: "list-table", style: "margin-top:0" }, h("tbody", {}, s.sessions.map((x) => h("tr", {},
          h("td", {}, x.ip, x.current ? h("span", { class: "pill g", style: "margin-left:6px" }, "지금 이 기기") : null, h("div", { class: "small faint" }, (x.ua || "").slice(0, 60))),
          h("td", { class: "r" }, fmt.time(x.last_seen)),
          h("td", { class: "r" }, x.current ? null : h("button", { class: "btn sm danger", onclick: async () => { await api("DELETE", `/api/system/sessions/${x.id}`); showSystem(); } }, "로그아웃")))))),
        h("div", { class: "section-title", style: "margin-top:18px" }, "최근 로그인 시도"),
        h("table", { class: "list-table", style: "margin-top:0" }, h("tbody", {}, s.attempts.slice(0, 10).map((a) => h("tr", {},
          h("td", {}, fmt.time(a.ts)), h("td", {}, a.ip), h("td", { class: "r" }, a.success ? h("span", { class: "pill g" }, "성공") : h("span", { class: "pill y" }, "실패"))))))),
      card("로그",
        h("div", { class: "row" }, logSel, h("button", { class: "btn sm", onclick: async () => { const r = await api("GET", `/api/logs?name=${logSel.value}&lines=300`); pre.textContent = r.text; pre.scrollTop = pre.scrollHeight; } }, "보기")),
        h("div", { style: "height:12px" }), pre)))));
}

// ------------------------------------------------------------------ 라우터
async function route() {
  closeModal();
  let sess;
  try { sess = await api("GET", "/api/session"); } catch (e) { return; }
  if (!sess.authenticated) { showLogin(); return; }
  if (sess.must_change) { mount(shell("lives")); showChangePassword(true); return; }
  const r = location.hash.replace(/^#\/?/, "");
  if (r.startsWith("media/video")) return showMedia("video");
  if (r.startsWith("media/audio")) return showMedia("audio");
  if (r.startsWith("system")) return showSystem();
  return showLives();
}
window.addEventListener("hashchange", route);
route();
