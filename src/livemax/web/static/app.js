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
  // 라이브 카드 '방송 시간' 칸용: 어떤 값이어도 한 줄 (10일 넘으면 일 단위만 — 150일도 넘치지 않게)
  age(sec) {
    if (sec == null || sec < 0) return "-";
    sec = Math.floor(sec); const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d >= 10) return `${d}일`; if (d) return `${d}일 ${hh}시간`; if (hh) return `${hh}:${pad(m)}:${pad(s)}`; return `${m}:${pad(s)}`;
  },
  // 트래픽은 업체 기준 10진법 (1TB = 1000GB) — 업체 청구·한도와 같은 숫자 (v2.14). 파일 크기 등은 아래 bytes 그대로
  bytesDec(b) { if (b == null) return "-"; const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; while (b >= 1000 && i < 5) { b /= 1000; i++; } return `${b.toFixed(i ? 1 : 0)} ${u[i]}`; },
  bytes(b) { if (b == null) return "-"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return `${b.toFixed(i ? 1 : 0)} ${u[i]}`; },
  num(v, d = 1, unit = "") { return v == null ? "-" : `${Number(v).toFixed(d)}${unit}`; },
};
const STATUS = { ok: "정상", warn: "경고", down: "중단", restarting: "재시작중", stopped: "대기중", waiting: "다음 방송 대기" };
const MODES = [["random_norepeat", "중복방지 랜덤"], ["random", "완전 랜덤"], ["sequential_loop", "순차 반복"], ["sequential", "순차 1회"]];
const ICON = {
  edit: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  out: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>',
  trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14"/></svg>',
  music: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  video: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="14" height="14" rx="2.5"/><path d="m16 10 6-3.5v11L16 14"/></svg>',
  timer: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9 2h6"/></svg>',
  plus: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  key: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8M17 6l3 3M14.5 8.5l2 2"/></svg>',
  warn: '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 1 21h22L12 2zm1 15h-2v-2h2v2zm0-4h-2V9h2v4z"/></svg>',
  grid: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  cctv: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="6" height="4" rx=".8"/><rect x="9" y="4" width="6" height="4" rx=".8"/><rect x="16" y="4" width="6" height="4" rx=".8"/><rect x="2" y="10" width="6" height="4" rx=".8"/><rect x="9" y="10" width="6" height="4" rx=".8"/><rect x="16" y="10" width="6" height="4" rx=".8"/><path d="M5 18h14"/></svg>',
  list: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="5" height="4" rx="1"/><rect x="3" y="10" width="5" height="4" rx="1"/><rect x="3" y="16" width="5" height="4" rx="1"/><path d="M11 6h10M11 12h10M11 18h10"/></svg>',
  link: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/></svg>',
  sun: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2" fill="currentColor" fill-opacity=".25"/><path d="M12 2v2.5M12 19.5V22M4.2 4.2l1.8 1.8M18 18l1.8 1.8M2 12h2.5M19.5 12H22M4.2 19.8 6 18M18 6l1.8-1.8"/></svg>',
  cloud: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 18h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 9.2 4.4 4.4 0 0 0 7 18z" fill="currentColor" fill-opacity=".2"/></svg>',
  storm: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 15.5h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.6 6.7 4.4 4.4 0 0 0 7 15.5z" fill="currentColor" fill-opacity=".2"/><path d="m12.5 13-2.5 4h3.5l-2.5 4"/></svg>',
  idle: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/></svg>',
  film: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4"/></svg>',
};
const ico = (name, cls) => h("span", { class: "ico " + (cls || ""), html: ICON[name] });

function toast(msg, isErr) {
  const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  $("#toast-root").append(t);
  setTimeout(() => t.remove(), isErr ? 7000 : 3500);
}

// 서버가 잠깐 늦거나 끊겨도 화면이 하얗게 비지 않게: 요청마다 시간 제한 + 위쪽 안내 띠
function netBanner(on) {
  let b = document.getElementById("net-banner");
  if (!on) { if (b) b.remove(); return; }
  if (!b) { b = h("div", { id: "net-banner" }, "서버 응답이 늦습니다 — 자동으로 다시 시도 중…"); document.body.append(b); }
}

async function api(method, url, body, raw, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs || (body instanceof Blob ? 600000 : 20000));
  const opt = { method, headers: {}, credentials: "same-origin", signal: ctl.signal };
  if (body instanceof Blob || body instanceof ArrayBuffer) { opt.body = body; }
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, opt); }
  catch (e) { netBanner(true); throw new Error(e.name === "AbortError" ? "서버 응답 시간 초과 — 잠시 뒤 다시 시도하세요" : "서버에 연결할 수 없습니다"); }
  finally { clearTimeout(timer); }
  netBanner(r.status >= 502 && r.status <= 504);
  if (raw) return r;
  let j = {};
  try { j = await r.json(); } catch (e) { /* 비어 있는 응답 */ }
  if (r.status === 401 && url !== "/api/login") { showLogin(); throw new Error(j.error || "로그인이 필요합니다"); }
  if (r.status === 428) { showChangePassword(true); throw new Error(j.error); }
  if (!r.ok && r.status !== 409) throw new Error(j.error || `오류 (${r.status})`);
  j._status = r.status;
  return j;
}

// ------------------------------------------------------------------ 서버명 (로고 옆·창 제목)
let SERVER_NAME = "";
function setServerName(n) {
  SERVER_NAME = (n || "").trim();
  document.title = SERVER_NAME ? `LiveMax ${SERVER_NAME}` : "LiveMax";
}
const brandParts = () => [h("img", { class: "logo", src: "/assets/logo.png", alt: "" }), h("b", {}, "LiveMax"), SERVER_NAME ? h("span", { class: "srv" }, SERVER_NAME) : null];

// ------------------------------------------------------------------ 레이아웃
let pollTimer = null;
function stopPoll() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

function shell(active, ...content) {
  const nav = [["#/", "라이브", "lives"], ["#/media/video", "비디오", "video"], ["#/media/audio", "음악", "audio"], ["#/channels", "채널", "channels"], ["#/system", "설정", "system"]];
  return h("div", {},
    h("header", { class: "topbar" },
      h("a", { class: "brand", href: "#/" }, brandParts()),
      h("nav", { class: "nav" }, nav.map(([href, label, key]) => h("a", { href, class: active === key ? "on" : "" }, label)))),
    h("main", { class: "page" }, content));
}

function mount(node) { $("#app").replaceChildren(node); }

async function logout() { try { await api("POST", "/api/logout"); } catch (e) { /* 무시 */ } showLogin(); }

// ------------------------------------------------------------------ 로그인 / 비밀번호
function showLogin() {
  stopPoll(); closeAllModals();
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
    h("div", { class: "brand" }, brandParts()),
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
// 창은 보통 하나씩 (새 창이 앞 창을 대신함). 단, 관제 상세 창(cam-modal) 위에서 연 창(로그·설정 등)은 그 위에 겹쳐 띄워서
// 닫으면 상세 창이 그대로 남는다 (v2.14). 창이 떠 있는 동안 뒤 페이지는 스크롤되지 않게 잠근다.
const modalRoot = () => $("#modal-root");
const modalTop = () => modalRoot().lastElementChild;
function syncModalLock() { document.documentElement.classList.toggle("modal-open", !!modalTop()); }
function openModal(title, body, foot, opts = {}) {
  const cam = modalRoot().querySelector(".cam-modal");
  if (cam && !opts.cls?.includes("cam-modal")) { while (modalTop() !== cam) modalTop().remove(); }  // 상세 창 위에 겹침
  else closeAllModals();
  // 창 밖을 눌러도 닫히지 않는다 (입력하던 내용이 날아가지 않게) — X 또는 취소 버튼으로만 닫힘.
  // 예외: 보기 전용 창(opts.backdrop — 관제 상세)은 바깥을 누르거나 ESC 로도 닫힌다
  const back = h("div", { class: "modal-back" + (opts.cls ? " " + opts.cls : "") },
    h("div", { class: "modal", style: opts.wide ? "width:min(1000px,100%)" : "" },
      h("div", { class: "card-head" }, title, h("span", { class: "spacer" }),
        opts.locked ? null : h("button", { class: "iconbtn", style: "width:28px;height:28px;background:transparent", onclick: closeModal }, "✕")),
      h("div", { class: "modal-body" }, body),
      foot ? h("div", { class: "modal-foot" }, foot) : null));
  if (opts.backdrop) back.addEventListener("click", (e) => { if (e.target === back) closeModal(back); });
  if (opts.backdrop) back.dataset.esc = "1";
  modalRoot().append(back);
  syncModalLock();
}
// 맨 위 창 하나만 닫는다 (el 을 주면 그 창). 상세 창 위의 로그를 닫으면 상세 창이 남는다
function closeModal(el) {
  const b = el instanceof Element ? el : modalTop();
  if (!b) return;
  if (b.classList.contains("cam-modal")) CAM_OPEN = null;
  b.remove();
  syncModalLock();
}
function closeAllModals() { modalRoot().replaceChildren(); CAM_OPEN = null; syncModalLock(); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalTop()?.dataset.esc === "1") closeModal(); });

// ------------------------------------------------------------------ 라이브 (메인)
let lastOverview = null;

// 같은 화면이면 통째로 다시 그리지 않고 내용만 갱신한다 (재시작·저장 뒤 깜빡임 없이 자연스럽게)
const PAGE = { name: null, refresh: null, root: null };
const samePage = (name) => PAGE.name === name && PAGE.root && document.contains(PAGE.root);
function softRefresh() { if (PAGE.refresh && PAGE.root && document.contains(PAGE.root)) PAGE.refresh(); else route(); }
// 썸네일은 같은 그림이면 같은 <img> 를 다시 쓴다 — 갱신 때마다 새로 만들면 그림이 깜빡인다
const IMG = new Map();
function cimg(src, cls, key) {
  const k = `${key || ""}|${cls || ""}|${src}`;
  let el = IMG.get(k);
  if (!el) {
    el = h("img", { class: cls || null, src, alt: "", loading: "lazy" });
    IMG.set(k, el);
    if (IMG.size > 500) IMG.delete(IMG.keys().next().value);
  }
  return el;
}

async function showLives() {
  if (samePage("lives")) return PAGE.refresh();
  stopPoll();
  const grid = h("div", { class: "groups" });
  const side = h("aside", { class: "side" });
  mount(shell("lives", h("div", { class: "layout" }, h("section", {}, grid), side)));
  let busy = false;
  const refresh = async () => {
    if (busy || document.hidden) return;  // 앞 요청이 아직이거나 화면을 안 보고 있으면(다른 탭·폰 잠금) 서버를 부르지 않는다
    busy = true;
    try { lastOverview = await api("GET", "/api/overview"); renderLives(grid, lastOverview); renderSide(side, lastOverview); }
    catch (e) { if (!lastOverview) grid.replaceChildren(h("div", { class: "card empty" }, h("div", { class: "card-body muted" }, e.message))); }
    finally { busy = false; }
  };
  PAGE.name = "lives"; PAGE.refresh = refresh; PAGE.root = grid;
  if (lastOverview) { renderLives(grid, lastOverview); renderSide(side, lastOverview); }  // 마지막 모습부터 바로 보여 주고 뒤에서 갱신
  else grid.replaceChildren(h("div", { class: "muted small", style: "padding:20px" }, "불러오는 중…"));
  await refresh();
  pollTimer = setInterval(refresh, 5000);
  document.onvisibilitychange = () => { if (!document.hidden && document.contains(grid)) refresh(); };  // 다시 보면 바로 갱신
}

// 채널 로고(없으면 첫 글자) + 이름. 주소가 있으면 새 창으로 연다
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
function chLogo(ch, size) {
  return ch.logo ? h("img", { class: "ch-logo", src: ch.logo, alt: "", style: `width:${size}px;height:${size}px` })
    : h("span", { class: "ch-logo ph", style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.45)}px` }, (ch.name || "?").slice(0, 1));
}
function chLink(ch, cls, ...kids) {
  const u = safeUrl(ch.url);
  return u ? h("a", { class: cls, href: u, target: "_blank", rel: "noopener noreferrer", title: `${ch.name} 채널 열기 (새 창)` }, kids)
    : h("span", { class: cls }, kids);
}

// 관제 보기 상세 창: 칸을 두 번 누르면 블록 보기 카드(버튼·로그·설정 포함)를 그대로 띄운다 — 버튼 안전장치도 같음
const CAM_TAP = { id: null, t: 0 };
let CAM_OPEN = null, CAM_CARD = null;
function camDetailBody(lv) {
  const why = (lv.status === "warn" || lv.status === "down") && lv.message
    ? h("div", { class: "msg err", style: "margin-bottom:10px" }, lv.message) : null;
  return h("div", {}, why, CAM_CARD ? CAM_CARD(lv) : null);
}
function openCamDetail(id) {
  const lv = (lastOverview?.lives || []).find((x) => x.id === id);
  if (!lv || !CAM_CARD) return;
  openModal(`상세 — ${lv.name}`, h("div", { class: "cam-detail" }, camDetailBody(lv)), null, { backdrop: true, cls: "cam-modal" });
  CAM_OPEN = id;  // openModal 이 앞 창을 닫으며 비우므로 그 뒤에
}

// 라이브 보기 방식: grid(블록, 기본) | list(목록, 쇼핑몰처럼) | cctv(관제 — 썸네일만 작게 한 줄 5개). 서버 설정에 저장되어 PC·휴대폰 어디서나 같게
let LIVES_VIEW = "grid";

function renderLives(grid, ov) {
  const now = ov.server_time;
  const chById = Object.fromEntries((ov.channels || []).map((c) => [c.id, c]));
  const parts = (lv) => {
    const running = lv.state === "running";
    const ch = chById[lv.channel_id];
    const bad = lv.status === "warn" || lv.status === "down";
    const tile = (val, label, cls) => h("div", { class: "tile " + (cls || "") }, h("b", {}, val), h("span", {}, label));
    const speedCls = lv.speed == null ? "" : lv.speed >= 0.97 ? "good" : "bad";
    const statusEl = () => h("span", { class: "status " + lv.status }, bad ? ico("warn") : null,
      lv.status === "restarting" ? reLabel(lv) : (STATUS[lv.status] || lv.status));
    // 썸네일 그림 (블록·목록·관제 보기가 같이 씀). key 는 보기마다 <img> 를 따로 두려고
    const pic = (key, short) => running && lv.clip && lv.clip_folder
        ? cimg(`/api/thumbs/${encodeURIComponent(lv.clip_folder)}/${encodeURIComponent(lv.clip)}?v=${lv.clip_v || 0}`, null, lv.id + key)
        : !running && lv.idle_thumb  // 송출 전: 고른 비디오의 첫 클립을 반투명으로
          ? [cimg(`/api/thumbs/${encodeURIComponent(lv.idle_thumb.folder)}/${encodeURIComponent(lv.idle_thumb.name)}?v=${lv.idle_thumb.v || 0}`, "idle", lv.id + key),
             lv.desired ? null : h("span", { class: "idle-tag" }, "송출 전")]  // 송출하려는데 끊긴 라이브는 왼쪽 위 '중단' 표시로
          : h("div", { class: "ph" }, running ? (short ? "준비 중" : "미리보기 준비 중") : (short ? "꺼짐" : "송출 중이 아닙니다"));
    return {
      running, ch,
      pic,
      age: running && (lv.session_started_at || lv.started_at) ? fmt.age(now - (lv.session_started_at || lv.started_at)) : null,
      bad,
      preview: (withBadge) => h("div", { class: "preview" }, pic(withBadge ? "g" : "l"), liveBadge(lv, running),
        withBadge && ch ? chLink(ch, "ch-badge", chLogo(ch, 24), h("span", {}, ch.name)) : null),
      status: (() => {
        const el = statusEl();
        return bad || lv.status === "restarting" ? logClick(lv, el) : el;
      })(),
      statusPlain: statusEl(),  // 관제 칸: 누르면 로그가 바로 열리지 않게 (두 번 누르기와 겹침)
      title: h("span", { class: "lv-title" }, lv.name),
      keytag: lv.key_label ? h("span", { class: "keytag", title: `스트림 키: ${lv.key_label} (${lv.key_masked})` }, ico("key"), lv.key_label) : null,
      edit: h("button", { class: "iconbtn", style: "width:30px;height:30px;background:transparent", title: "설정", html: ICON.edit, onclick: () => openLiveEditor(lv) }),
      tiles: h("div", { class: "tiles" },
        tile(running && (lv.session_started_at || lv.started_at) ? fmt.age(now - (lv.session_started_at || lv.started_at)) : "-", "방송 시간"),
        tile(running && lv.bitrate_kbps ? Math.round(lv.bitrate_kbps).toLocaleString() : "-", "kbit/s"),
        tile(lv.speed != null ? lv.speed.toFixed(2) + "x" : "-", "speed", speedCls)),
      lines: [
        h("div", { class: "nowline" }, ico("music", "lbl"), running && lv.song_title
          ? h("span", { class: "val" }, h("span", { class: "fold" }, lv.song_folder || ""), h("span", { class: "sep" }, "|"),
              h("b", {}, lv.song_title + (lv.song_artist ? " — " + lv.song_artist : "")))
          : h("span", { class: "val" }, "-")),
        h("div", { class: "nowline" }, ico("video", "lbl"), running && lv.clip
          ? h("span", { class: "val" }, h("span", { class: "fold" }, lv.clip_folder || ""), h("span", { class: "sep" }, "|"), h("span", {}, lv.clip))
          : h("span", { class: "val" }, "-")),
        timerLine(lv, now),
        lv.message ? h("div", { class: "msg" + (lv.status === "down" ? " err" : "") }, lv.message) : null],
      actions: h("div", { class: "actions" },
        // 송출 중(또는 타이머 반복 대기)일 때만 정지·재시작, 아니면 시작
        lv.desired || lv.status === "waiting"
          ? armBtn(lv, "stop", "btn danger", lv.status === "waiting" ? "반복 끄고 정지" : "정지")
          : h("button", { class: "btn primary", onclick: () => liveAction(lv, "start") }, "시작"),
        lv.desired ? armBtn(lv, "restart", "btn", "재시작") : h("button", { class: "btn", disabled: true }, "재시작")),
      links: h("div", { class: "links" },
        reconnects(lv),
        // 새 중계기가 기본이라 평소엔 표시 없음 — 기존 방식으로 나가고 있을 때만 (켜 둔 경우·자동 전환)
        lv.state === "running" && lv.transport && lv.transport !== "lm-relay"
          ? h("span", { class: "warnline", title: lv.fast_relay === false ? "설정에서 기존 방식을 켜 둠" : "새 중계기 연속 실패로 30분간 자동 전환" }, "기존 방식 송출") : null,
        lv.youtube_video_id ? h("a", { onclick: () => openPreview(lv) }, "유튜브 미리보기") : null,
        h("a", { onclick: () => openLog(lv) }, "로그"),
        h("span", { class: "faint" }, lv.timer_mode !== "off" ? "타이머 방송 — 일일 재시작 안 함" : `다음 자동 재시작 ${fmt.time(lv.next_restart)}`)),
    };
  };
  const card = (lv) => {
    const p = parts(lv);
    return h("article", { class: "card live" },
      h("div", { class: "card-head" }, p.status, p.title, p.keytag, h("span", { class: "spacer" }), p.edit),
      p.preview(true),
      h("div", { class: "card-body" }, p.tiles, p.lines, p.actions, p.links));
  };
  const row = (lv) => {  // 목록 보기: 작은 미리보기 | 이름·상태·지금 곡/클립 | 숫자 | 버튼
    const p = parts(lv);
    return h("article", { class: "card live row-live" },
      h("div", { class: "rl-thumb" }, p.preview(false)),
      h("div", { class: "rl-main" },
        h("div", { class: "rl-head" }, p.status, p.title, p.keytag, h("span", { class: "spacer" }), p.edit),
        p.lines, p.links),
      h("div", { class: "rl-side" }, p.tiles, p.actions));
  };
  // 관제 보기(CCTV): 썸네일 위주로 한눈에. 그림 안 = 상태 표시(작게) + 오른쪽 아래 방송 시간, 그림 아래 = 이름·키·상태.
  // 문제(경고·중단)면 그림 위에 빨간 반투명이 깜빡인다. 시작 중·재시작 중은 정상 과정이라 빨갛게 하지 않는다
  const cam = (lv) => {
    const p = parts(lv);
    const art = h("article", { class: "cam" + (p.bad ? " bad " + lv.status : "") + (lv.desired ? "" : " off"),
        title: `${lv.name}${lv.message ? "\n" + lv.message : ""}` },
      // 그림 안: 왼쪽 위 LIVE(정상 송출일 때만) · 오른쪽 위 상태(검은 바탕) · 오른쪽 아래 방송 시간. 그림 아래는 이름·키만
      h("div", { class: "cam-view" }, p.pic("c", true),
        lv.desired && p.running && lv.status === "ok" ? h("span", { class: "badge-live" }, "LIVE") : null,
        h("span", { class: "cam-st" }, p.statusPlain),
        p.age ? h("span", { class: "cam-time" }, p.age) : null,
        p.bad ? h("span", { class: "cam-alert" }) : null),
      h("div", { class: "cam-cap" },
        h("div", { class: "cam-name" }, h("span", { class: "lv-title" }, lv.name), p.keytag)));
    // 두 번 누르면 상세 창 (휴대폰에서 스크롤하다 잘못 눌러도 안 열리게 한 번 누름은 아무 일 없음)
    art.title += "\n두 번 눌러 상세 보기·조작";
    art.addEventListener("click", () => {
      const t = Date.now();
      if (CAM_TAP.id === lv.id && t - CAM_TAP.t < 350) { CAM_TAP.id = null; openCamDetail(lv.id); return; }
      CAM_TAP.id = lv.id; CAM_TAP.t = t;
    });
    return art;
  };
  CAM_CARD = card;
  if (CAM_OPEN && $("#modal-root .cam-detail")) {  // 상세 창이 떠 있으면 같은 주기로 내용도 새로
    const lv = ov.lives.find((x) => x.id === CAM_OPEN);
    if (lv) $("#modal-root .cam-detail").replaceChildren(camDetailBody(lv));
    else closeModal($("#modal-root .cam-modal"));
  }
  // 채널별로 묶어서: 채널 머리줄(로고·이름 ──── 설명) 아래에 그 채널의 라이브들
  const groups = (ov.channels || []).map((c) => ({ ch: c, lives: ov.lives.filter((l) => l.channel_id === c.id) }))
    .filter((g) => g.lives.length);
  const orphan = ov.lives.filter((l) => !chById[l.channel_id]);
  if (orphan.length) groups.push({ ch: null, lives: orphan });
  const body = (lives) => LIVES_VIEW === "list" ? h("div", { class: "rows" }, lives.map(row))
    : LIVES_VIEW === "cctv" ? h("div", { class: "cams" }, h("div", { class: "cam-grid" }, lives.map(cam)))
    : h("div", { class: "grid" }, lives.map(card));
  const sections = groups.map((g) => h("section", { class: "ch-group" },
    h("div", { class: "ch-head" },
      g.ch ? chLink(g.ch, "ch-name", chLogo(g.ch, 40), h("span", {}, g.ch.name)) : h("span", { class: "ch-name muted" }, "채널 미지정"),
      h("span", { class: "ch-count", title: "이 채널의 라이브 수 (유튜브는 채널당 동시 송출 10개까지)" }, `${g.lives.length}개`),
      h("span", { class: "ch-rule" }),
      g.ch && g.ch.description ? h("span", { class: "ch-desc" }, g.ch.description) : null),
    body(g.lives)));
  if (!sections.length) sections.push(h("div", { class: "card empty" }, h("div", { class: "card-body" },
    h("b", {}, "아직 라이브가 없습니다"), h("p", { class: "muted" }, "① 채널 메뉴에서 채널과 스트림 키 등록 → ② 비디오·음악 올리기 → ③ '라이브 추가'"))));
  const viewBtn = (v, label) => h("button", { type: "button", class: LIVES_VIEW === v ? "on" : "", title: label, "aria-label": label, html: ICON[v],
    onclick: async () => {
      if (LIVES_VIEW === v) return;
      LIVES_VIEW = v; renderLives(grid, ov);
      try { await api("PUT", "/api/system/settings", { lives_view: v }); } catch (e) { /* 표시만 바뀌고 저장은 다음에 */ }
    } });
  // 맨 위 한 줄: [미니 관제 | 보기 전환]. 좁은 화면에서는 관제 → 라이브 추가 → (라이브 N개 · 보기 전환) 순서로 쌓인다
  const bar = ov.lives.length ? h("div", { class: "viewbar" }, pulseBox(ov), addBtn("m-only"),
    h("span", { class: "vb-count muted small" }, `라이브 ${ov.lives.length}개`), h("span", { class: "spacer" }),
    h("div", { class: "seg" }, viewBtn("grid", "블록 보기"), viewBtn("list", "목록 보기"), viewBtn("cctv", "관제 보기 (CCTV)"))) : addBtn("m-only");
  grid.replaceChildren(bar, ...sections);
}
const addBtn = (cls) => h("button", { class: "add-btn " + cls, onclick: () => {
  const ov = lastOverview, max = ov?.max_lives;
  if (max && ov.lives.length >= max) { toast(`이 서버의 라이브 최대 개수(${max}개)에 도달했습니다 — 설정 > 서버에서 늘리거나 안 쓰는 라이브를 지우세요`, true); return; }
  openLiveEditor(null);
} }, h("span", { class: "plus", html: ICON.plus }), "라이브 추가");

// 켜지는 중 표시: 처음 켤 때(시작 버튼·부팅·업데이트)는 '시작 중', 일부러 바꾸는 중이면 '영상 교체 중', 끊겨서 다시 켜면 '재시작중'
const reLabel = (lv) => (lv.starting ? "시작 중" : lv.planned ? "영상 교체 중" : "재시작중");

// 미리보기 왼쪽 위 표시: 정상 LIVE(빨강) · 경고(주황) · 재시작중(하늘) · 중단(검정+빨간 테두리). 송출 안 하는 라이브는 없음
function liveBadge(lv, running) {
  if (!lv.desired) return null;
  if (lv.status === "ok" && running) return h("span", { class: "badge-live" }, "LIVE");
  const [cls, text] = { warn: ["warn", "경고"], restarting: ["re", reLabel(lv)], down: ["down", "중단"] }[lv.status] || [null, null];
  return cls ? h("span", { class: "badge-live " + cls }, cls === "re" ? null : ico("warn"), text) : null;
}

// 일부러 한 재시작(매일 자동·설정 저장·재시작 버튼)은 끊김으로 세지 않는다 — 진짜 문제만
const PLANNED = /^(일일 재시작|설정 적용|사용자 재시작)/;
// 유튜브가 끊고 5초 안에 다시 붙은 것(유튜브 정비 등) — 시청자 영향 없음: 끊김으로 세지 않고 카드에 회색으로만 (DESIGN 11-D 11)
const QUIET = /^유튜브 짧은 재연결/;
// 카드·관제의 횟수는 모두 '하루' 기준 — 일일 재시작 시각(기본 한국 자정)에 0 으로 (자세한 기록은 로그에 남음)
const dayStart = () => lastOverview?.day_start || 0;
const sessionR = (lv) => (lv.session_started_at ? (lv.restarts || []).filter((r) => r.ts >= Math.max(lv.session_started_at, dayStart())) : []);
const drops = (lv) => sessionR(lv).filter((r) => !PLANNED.test(r.reason || "") && !QUIET.test(r.reason || ""));
const quietDrops = (lv) => sessionR(lv).filter((r) => QUIET.test(r.reason || ""));

// 라이브 화면 맨 위 미니 관제: 가동률(송출하려는 라이브 중 정상 비율)과 상태 수. 맑음(파랑)/주의(보라)/심각(빨강)
// 미니 관제·시스템 칸이 같은 기준으로 센다.
// 송출 대상 = 켜져 있는 라이브 + 켜기로 했지만 아직 순서를 기다리는 라이브(업데이트·부팅 직후). 타이머 '다음 방송 대기'는 제외.
// 재시작·시작 중은 경고가 아니다 (정상 과정) — 따로 '재시작'으로.
function liveCounts(ov) {
  const active = ov.lives.filter((l) => l.desired || (l.enabled && l.status !== "waiting"));
  const n = { ok: 0, warn: 0, down: 0, re: 0, st: 0, active, idle: ov.lives.length - active.length };
  active.forEach((l) => {
    if (l.status === "ok") n.ok++;
    else if (l.status === "warn") n.warn++;
    else if (l.status === "down") n.down++;
    else if (l.starting || !l.desired) n.st++;  // 처음 켜지는 중 · 켜기로 했지만 순서를 기다리는 중
    else n.re++;
  });
  return n;
}

function pulseBox(ov) {
  const n = liveCounts(ov);
  const active = n.active, idle = n.idle;
  // 끊김은 '하루'(일일 재시작 기준 시각, 기본 한국 자정부터) 동안의 진짜 끊김 — 일부러 한 재시작·업데이트·유튜브 짧은 재연결 제외
  const dropCnt = active.reduce((a, l) => a + drops(l).length, 0);
  const level = !active.length ? "idle" : n.down ? "bad" : n.warn ? "warn" : "good";
  const pct = active.length ? Math.floor((n.ok / active.length) * 100) : null;
  const icon = { good: "sun", warn: "cloud", bad: "storm", idle: "idle" }[level];
  const cpu = ov.system?.cpu_pct;
  const mt = ov.system?.mem_total_mb, mu = ov.system?.mem_used_mb;
  const mem = mt && mu != null ? (mu / mt) * 100 : null;
  const memHot = mem != null && mu > mt - (ov.capacity?.mem_reserve_mb ?? 700);  // 안전선(남길 여유)을 넘으면 강조
  const stat = (k, v, cls, title, sub) => h("span", { class: "ps " + (cls || ""), title: title || null }, k, " ", h("b", {}, v),
    sub ? h("small", { class: "ps-sub" }, sub) : null);
  return h("div", { class: "pulse " + level, role: "status" },
    h("span", { class: "p-ico", html: ICON[icon] }),
    h("div", { class: "p-title" }, pct == null ? "준비중" : ["가동률 ", h("b", {}, `${pct}%`)]),
    cpu != null || mem != null ? h("span", { class: "p-res" },
      cpu != null ? h("span", { class: "p-cpu" + (cpu >= 85 ? " hot" : ""), title: "서버 전체 CPU 사용량" }, "CPU ", h("b", {}, `${Math.round(cpu)}%`)) : null,
      mem != null ? h("span", { class: "p-cpu" + (memHot ? " hot" : ""), title: `메모리 점유율 (${fmt.num(ov.system.mem_used_mb / 1024, 1)} / ${fmt.num(ov.system.mem_total_mb / 1024, 1)} GB)` },
        "메모리 ", h("b", {}, `${Math.round(mem)}%`)) : null) : null,
    h("div", { class: "p-stats" },
      stat("라이브", active.length), stat("정상", n.ok), stat("경고", n.warn, n.warn ? "hot" : ""), stat("중단", n.down, n.down ? "hot" : ""),
      stat("끊김", dropCnt, dropCnt ? "hot" : "", "오늘(일일 재시작 시각, 기본 자정부터) 끊긴 횟수 — 일부러 한 재시작·업데이트·5초 안의 유튜브 재연결 제외. 어느 라이브인지는 각 카드에 (누르면 로그)", "(1일)"),
      n.st ? stat("시작 중", n.st, "dim", "켜지는 중 (시작 버튼·업데이트 직후 순서대로 — 정상 과정)") : null,
      n.re ? stat("재시작", n.re, "dim", "다시 켜지는 중 (끊겨서 자동으로 다시 켜거나 재시작 버튼)") : null,
      idle ? stat("대기", idle, "dim", "꺼 둔 라이브") : null));
}

// 이번 방송(세션) 동안 자동 재연결·재시작 횟수 — 가동 시간 대신 "몇 번 끊겼다 붙었나"를 보여준다
function reconnects(lv) {
  if (!lv.session_started_at) return null;
  const list = drops(lv), quiet = quietDrops(lv), b = lv.blips_day;
  const tip = (rs) => rs.map((r) => `${fmt.time(r.ts)} ${r.reason}`).join("\n");
  const out = [];
  if (list.length) {
    const last = list[list.length - 1];
    out.push(logClick(lv, h("span", { class: "warnline", title: tip(list) }, `재연결 ${list.length}회 (1일 · 최근 ${fmt.clock(last.ts)})`)));
  }
  // 아래 둘은 알림 없이 회색으로만 — 시청자가 못 느끼는 수준 (유튜브 쪽 출렁임)
  if (quiet.length) out.push(logClick(lv, h("span", { class: "faint", title: tip(quiet) + "\n(5초 안에 다시 붙음 — 시청자 영향 없음)" }, `유튜브 재연결 ${quiet.length}회 (1일)`)));
  if (b && b.n) out.push(logClick(lv, h("span", { class: "faint", title: "오늘(일일 재시작 시각부터) 1초 넘게 밀린 횟수 (유튜브 쪽 출렁임 — 알림 없음)" }, `밀림 ${b.n}회 · 최대 ${fmt.num(b.max, 1)}초 (1일)`)));
  if (!out.length) return h("span", { class: "faint" }, "끊김 없음");
  return h("span", { class: "drops" }, out);
}

function timerLine(lv, now) {
  if (lv.timer_mode === "off") return null;
  let text;
  if (lv.next_start_at) text = `다음 방송 ${fmt.time(lv.next_start_at)} 시작 (${fmt.dur(Math.max(0, lv.next_start_at - now))} 뒤)`;
  else if (lv.timer_end_at) text = `${fmt.time(lv.timer_end_at)} 자동 종료 · 남은 시간 ${fmt.dur(Math.max(0, lv.timer_end_at - now))}`;
  else if (lv.timer_mode === "duration") text = `시작하면 ${fmtHM(lv.timer_minutes)} 뒤 자동 종료${lv.timer_repeat ? " · 반복" : ""}`;
  else text = `${fmt.time(lv.timer_until)} 자동 종료`;
  return h("div", { class: "nowline timer" }, ico("timer", "lbl"), h("span", { class: "val" }, text));
}
function toLocalInput(ts) { const d = new Date(ts * 1000); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function fmtHM(min) { const hh = Math.floor(min / 60), mm = min % 60; return hh ? `${hh}시간${mm ? " " + mm + "분" : ""}` : `${mm}분`; }

// 오른쪽 서버 관제 칸 고정 (v2.14) — 이 브라우저에 기억. 고정하면 칸 높이를 화면에 맞추고 넘치는 부분은 칸 안에서 스크롤
let SIDE_PIN = (() => { try { return localStorage.getItem("lm.sidePin") === "1"; } catch (e) { return false; } })();
function setSidePin(v) {
  SIDE_PIN = v;
  try { localStorage.setItem("lm.sidePin", v ? "1" : "0"); } catch (e) { /* 무시 */ }
  document.querySelectorAll(".side").forEach((el) => el.classList.toggle("pinned", v));
}
function renderSide(side, ov) {
  side.classList.toggle("pinned", SIDE_PIN);
  // 고정 위치 = 원래 자리(상단바 실제 높이 + 본문 위 여백) — 스크롤을 시작할 때 칸이 위로 밀리지 않게 (v2.15)
  const tb = document.querySelector(".topbar");
  if (tb) document.documentElement.style.setProperty("--topbar-h", `${tb.offsetHeight}px`);
  const keepTop = side.scrollTop;  // 5초마다 다시 그려도 칸 안 스크롤 위치 유지
  const s = ov.system || {}, c = ov.capacity || {};
  const counts = liveCounts(ov);
  // 서버 박스 한 줄: 이름 · 지금 값 / 막대(실제 사용 ÷ 전체, 세로 눈금 = 안전선) / 설명. 먼저 막히는 자원은 이름 굵게
  // 안전선 넘으면 노랑·95% 넘으면 빨강 + 빗금(색만으로 구분하지 않게 — 색약 배려), 값 앞에 ⚠
  const over = (frac, safe) => (frac || 0) >= 0.95 ? "hi" : safe != null && (frac || 0) > safe ? "mid" : "";
  // peak: 오늘 최고값 위치 (0~1) — 안전선과 같은 모양의 빨간 세로선 (v2.15)
  const bar = (frac, safe, kind, peak) => {
    const f = Math.max(0, Math.min(1, frac || 0));
    const cls = (kind === "lives" ? "" : over(frac, safe)) || "k-" + kind;  // 라이브 개수 막대는 늘 초록 (꽉 차도 경고 아님)
    return h("div", { class: "cap-bar" }, h("i", { class: cls, style: `width:${(f * 100).toFixed(1)}%` }),
      safe != null ? h("s", { style: `left:${(Math.min(1, safe) * 100).toFixed(1)}%`, title: "안전선" }) : null,
      peak != null && peak > 0 ? h("u", { style: `left:${(Math.min(1, peak) * 100).toFixed(1)}%`, title: "오늘 최고" }) : null);
  };
  // 제목 옆 작은 회색 글씨 = 지금 사용률 % (CPU 는 값 자체가 % 라 생략)
  const row = (kind, k, v, note, frac, safe, lead, peak) => h("div", { class: "cap-row" + (lead ? " lead" : "") },
    h("div", { class: "cap-top" }, h("span", { class: "cap-k" }, k,
      kind !== "cpu" && frac != null ? h("small", { class: "cap-safe" }, `${Math.round(Math.max(0, frac) * 100)}%`) : null), h("b", { class: over(frac, safe) ? "over" : "" }, over(frac, safe) ? ico("warn") : null, v)),
    frac !== undefined ? bar(frac, safe, kind, peak) : null,
    note ? h("div", { class: "cap-note" }, note) : null);
  // 오늘 최고값 (1초 단위로 잰 순간 피크 — 위 숫자는 15초 평균). 하루 기준은 일일 재시작 시각
  const peakNote = (txt, at) => h("div", { class: "peak-note", title: "오늘(일일 재시작 시각부터) 1초 단위로 잰 순간 최고값. 위의 큰 숫자는 15초 평균이라 이보다 낮습니다" },
    "오늘 최고 ", h("b", {}, txt), at ? ` (${fmt.clock(at)})` : "");
  const memT = s.mem_total_mb, memU = s.mem_used_mb || 0, swapU = s.swap_used_mb || 0, reserve = c.mem_reserve_mb ?? 700;
  const cpuT = c.cpu_target_pct ?? 50, portR = c.port_use_ratio ?? 0.5, diskW = c.disk_warn_pct ?? 85;
  // 지금 송신 속도 그대로 한 달(이번 달 일수) 내내 보낸다면 — 대략 예상
  const sd = new Date((ov.server_time || Date.now() / 1000) * 1000);
  const mdays = new Date(sd.getUTCFullYear(), sd.getUTCMonth() + 1, 0).getDate();
  const monthBytes = s.net_tx_mbps != null ? (s.net_tx_mbps * 1e6 / 8) * mdays * 86400 : null;
  // 예상 최대 라이브 수: 메모리·CPU·회선·(한도 있으면)트래픽 안전선 중 가장 먼저 막히는 것 기준 — '최대 개수 설정'은 빼고
  const SHORT = { ram: "메모리", cpu: "CPU", bandwidth: "회선", traffic: "월 트래픽" };
  const opt = Object.entries(c.options || {}).filter(([k]) => SHORT[k]).sort((a, b) => a[1] - b[1])[0];
  const maxGuess = opt ? { n: (c.lives ?? ov.lives.length) + Math.max(0, opt[1]), by: SHORT[opt[0]] } : null;
  // 디스크 아래 4줄: 지금 올라와 있는 비디오·음악과, 디스크 안전선까지 더 올릴 수 있는 개수
  //   추가 비디오 = (5000k + 음악 128k) × 5분 ≈ 192MB 기준, 추가 음악 = 평균 3.2MB 기준
  const VIDEO_MB = (5000 + 128) * 300 / 8 / 1000, AUDIO_MB = 3.2;
  const diskFreeMB = s.disk_total_gb ? Math.max(0, s.disk_total_gb * diskW / 100 - (s.disk_used_gb || 0)) * 1000 : null;
  const mv = ov.media?.video || {}, ma = ov.media?.audio || {};
  const mb = (x) => `${Math.round((x || 0) / 1e6).toLocaleString()}MB`;
  const li = (...kids) => h("div", { class: "fit-li" }, "· ", ...kids);
  const diskFit = diskFreeMB != null ? h("div", { class: "cap-fit", title: "추가 개수는 디스크 안전선까지 남은 공간 기준" },
    li("현재 비디오 ", h("b", {}, (mv.n || 0).toLocaleString()), "개 (", mb(mv.bytes), ")"),
    li("추가 비디오 ", h("b", {}, Math.floor(diskFreeMB / VIDEO_MB).toLocaleString()), "개 (5분) 가능"),
    li("현재 음악 ", h("b", {}, (ma.n || 0).toLocaleString()), "개 (", mb(ma.bytes), ")"),
    li("추가 음악 ", h("b", {}, Math.floor(diskFreeMB / AUDIO_MB).toLocaleString()), "곡 (3.2MB) 가능")) : null;
  const st = [counts.st ? `시작 중 ${counts.st}` : null, counts.re ? `재시작 중 ${counts.re}` : null].filter(Boolean).join(" · ");
  side.replaceChildren(
    addBtn("d-only"),
    h("div", { class: "card" }, h("div", { class: "card-head" }, "서버", h("span", { class: "spacer" }),
      h("button", { class: "pin-btn" + (SIDE_PIN ? " on" : ""), "aria-pressed": SIDE_PIN ? "true" : "false",
        title: SIDE_PIN ? "고정 풀기 — 라이브 목록과 같이 스크롤" : "고정 — 라이브 목록을 내려도 이 칸은 그 자리에 (넓은 화면에서만)",
        onclick: () => { setSidePin(!SIDE_PIN); renderSide(side, ov); } }, SIDE_PIN ? "📌 고정됨" : "고정")),
      h("div", { class: "card-body" },
        h("div", { class: "big", style: "margin-bottom:10px" }, h("b", {}, c.can_add ?? "-"), h("span", {}, "추가 가능 (안전기준)")),
        h("div", { class: "cap-live" + (c.limiting === "max" ? " lead" : "") },
          h("span", { class: "cap-k" }, "라이브 ", h("b", {}, ov.max_lives ? `${ov.lives.length}/${ov.max_lives}` : `${ov.lives.length}`)),
          h("span", {}, "정상 ", h("b", {}, counts.ok), " · 경고 ", h("b", { class: counts.warn ? "hot" : "" }, counts.warn),
            " · 중단 ", h("b", { class: counts.down ? "hot" : "" }, counts.down), h("small", { class: "ps-sub", title: "지금 상태 (하루 누적 아님)" }, "(지금)")),
          st ? h("span", { class: "cap-st" }, st) : null,
          // 라이브 개수 막대 (최대 개수 대비) + 안전 기준으로 이 서버가 버틸 수 있는 총 라이브 수
          ov.max_lives ? h("div", { class: "cap-lbar" }, bar(ov.lives.length / ov.max_lives, null, "lives"),
            maxGuess ? h("div", { class: "cap-note cap-guess" },
              h("span", { title: "지금 상태로 안전선까지 — 메모리·CPU·회선 중 먼저 막히는 것 기준 ('최대 개수' 설정은 빼고)" },
                `안전기준: ${maxGuess.n}개 (${maxGuess.by})`),
              s.guess_low != null ? h("span", { class: "sep" }, "|") : null,
              s.guess_low != null ? h("span", { title: `오늘(일일 재시작 시각부터) 가장 나빴던 15초 기준 — ${fmt.clock(s.guess_low_at)} · ${SHORT[s.guess_low_by] || s.guess_low_by}. 라이브를 켜는 중·재시작 중인 순간은 뺌. 업데이트하면 다시 셈` },
                `최소기준 ${s.guess_low}개 (하루)`) : null) : null) : null),
        h("div", { class: "cap-rows" },
          row("mem", "메모리", memT ? `${fmt.num(memU / 1024, 1)} / ${fmt.num(memT / 1024, 1)} GB` : "-",
            // 남은 여유분은 스왑으로 옮겨진 몫까지 '쓰는 메모리'로 쳐서 계산 (여유가 부풀려 보이지 않게)
            memT ? [`라이브당 약 ${Math.round(c.mem_per_live_mb || 0)}MB · 남은 여유분 ${Math.max(0, Math.round(memT - reserve - memU - swapU)).toLocaleString()}MB`,
              swapU >= 50 ? h("span", { class: "swap-note", title: "리눅스가 한동안 안 쓰는 메모리를 비상 스왑으로 옮겨 둔 양 — 계속 오가지만 않으면 괜찮습니다 (오가면 알림)" },
                ` · 스왑 ${fmt.num(swapU / 1024, 1)}GB`) : null,
              s.mem_peak_mb ? peakNote(`${fmt.num(s.mem_peak_mb / 1024, 1)}GB`, s.mem_peak_at) : null] : null,
            memT ? memU / memT : 0, memT ? (memT - reserve) / memT : null, c.limiting === "ram", memT && s.mem_peak_mb ? s.mem_peak_mb / memT : null),
          row("cpu", "CPU", fmt.num(s.cpu_pct, 1, "%"), [`라이브당 약 ${fmt.num(c.cpu_per_live_pct, 1)}% · steal ${fmt.num(s.steal_pct, 2, "%")}`,
              s.cpu_peak_pct ? peakNote(`${fmt.num(s.cpu_peak_pct, 0)}%`, s.cpu_peak_at) : null],
            (s.cpu_pct || 0) / 100, cpuT / 100, c.limiting === "cpu", s.cpu_peak_pct ? s.cpu_peak_pct / 100 : null),
          row("net", "회선", `${fmt.num(s.net_tx_mbps, 0)} / ${fmt.num(c.port_mbit, 0)} Mbps`, `라이브당 ${fmt.num(c.per_live_mbps, 1)}Mbps`,
            c.port_mbit ? (s.net_tx_mbps || 0) / c.port_mbit : 0, portR, c.limiting === "bandwidth"),
          row("disk", "디스크", s.disk_total_gb ? `${fmt.num(s.disk_used_gb, 1)} / ${fmt.num(s.disk_total_gb, 0)} GB` : "-", diskFit,
            s.disk_total_gb ? (s.disk_used_gb || 0) / s.disk_total_gb : 0, diskW / 100, false)),
        h("div", { class: "cap-traffic" + (c.limiting === "traffic" ? " lead" : "") }, c.traffic_unlimited
          ? [h("div", { class: "cap-top" }, h("span", { class: "cap-k" }, "월 트래픽"), h("b", {}, "무제한")),
             h("div", { class: "cap-note" }, `이번 달 ${s.month_tx_bytes != null ? fmt.bytesDec(s.month_tx_bytes) : "-"}`),
             monthBytes != null ? h("div", { class: "cap-note" }, `지금 속도로 한 달 쓰면 약 ${fmt.bytesDec(monthBytes)}`) : null]
          : [h("div", { class: "cap-top" }, h("span", { class: "cap-k" }, "월 트래픽"), h("b", {}, `예상 ${fmt.num(c.projected_tb, 1)} / ${fmt.num(c.limit_tb, 0)} TB`)),
             bar(c.limit_tb ? c.projected_tb / c.limit_tb : 0, c.limit_tb ? c.target_tb / c.limit_tb : null, "traffic"),
             h("div", { class: "cap-note" }, `이번 달 ${fmt.num(c.month_tx_tb, 2)}TB · 라이브당 월 ${fmt.num(c.per_live_tb, 1)}TB`)]))),
    h("div", { class: "card", style: "margin-top:16px" }, h("div", { class: "card-head" }, "알림", h("span", { class: "spacer" }),
        ov.alerts.some((a) => a.id) ? h("button", { class: "linkbtn", onclick: () => clearAlerts() }, "모두 지우기") : null),
      h("div", { class: "card-body" }, ov.alerts.length
        ? h("ul", { class: "alerts" }, ov.alerts.slice(0, 12).map((a) => h("li", { class: a.level }, h("span", { class: "ic" }),
            (() => {
              const body = h("span", {}, h("time", {}, fmt.time(a.ts)), a.live ? h("b", {}, liveName(a.live) + " ") : null, a.message);
              const lv = a.live && ov.lives.find((x) => x.id === a.live);
              return lv ? logClick(lv, body) : body;
            })(),
            a.id ? h("button", { class: "x", title: "확인 — 지우기", "aria-label": "지우기", onclick: () => dismissAlert(a.id) }, "✕")
                 : h("span", { class: "x-ph", title: "지금 상태 — 해결되면 저절로 사라집니다" }))))
        : h("div", { class: "muted small" }, "알림 없음"))));
  if (keepTop) side.scrollTop = keepTop;
}

async function dismissAlert(id) {
  try { await api("DELETE", `/api/alerts/${id}`); showLives(); } catch (e) { toast(e.message, true); }
}
async function clearAlerts() {
  if (!confirm("확인한 알림을 모두 지울까요? (지금 진행 중인 문제는 해결될 때까지 남습니다)")) return;
  try { await api("DELETE", "/api/alerts"); showLives(); } catch (e) { toast(e.message, true); }
}

function liveName(id) { const l = (lastOverview?.lives || []).find((x) => x.id === id); return l ? l.name : id; }

// 실수 방지: 첫 번째 누름 → 게이지 50% + "한 번 더", 두 번째 누름(4초 안) → 확인 창. 화면이 5초마다 다시 그려져도 유지
const armed = {};
function armBtn(lv, action, cls, label) {
  const k = `${lv.id}:${action}`;
  const on = armed[k] && Date.now() - armed[k] < 4000;
  const b = h("button", { class: `${cls} arm${on ? " armed" : ""}` }, h("i", { class: "gauge" }), h("span", {}, on ? "한 번 더 누르세요" : label));
  b.addEventListener("click", () => {
    if (armed[k] && Date.now() - armed[k] < 4000) { delete armed[k]; liveAction(lv, action); return; }
    armed[k] = Date.now();
    b.classList.add("armed"); b.querySelector("span").textContent = "한 번 더 누르세요";
    setTimeout(() => { if (armed[k] && Date.now() - armed[k] >= 3900) { delete armed[k]; b.classList.remove("armed"); b.querySelector("span").textContent = label; } }, 4000);
  });
  return b;
}

// 같은 방식의 두 번 누르기를 다른 버튼에도 (예: 업데이트)
function armAny(k, cls, label, fn) {
  const on = armed[k] && Date.now() - armed[k] < 4000;
  const b = h("button", { class: `${cls} arm${on ? " armed" : ""}` }, h("i", { class: "gauge" }), h("span", {}, on ? "한 번 더 누르세요" : label));
  b.addEventListener("click", () => {
    if (armed[k] && Date.now() - armed[k] < 4000) { delete armed[k]; b.classList.remove("armed"); b.querySelector("span").textContent = label; fn(); return; }
    armed[k] = Date.now();
    b.classList.add("armed"); b.querySelector("span").textContent = "한 번 더 누르세요";
    setTimeout(() => { if (armed[k] && Date.now() - armed[k] >= 3900) { delete armed[k]; b.classList.remove("armed"); b.querySelector("span").textContent = label; } }, 4000);
  });
  return b;
}

async function liveAction(lv, action) {
  const label = { start: "시작", stop: "정지", restart: "재시작" }[action];
  if (action !== "start" && !confirm(`'${lv.name}' 을(를) ${label}할까요?\n${action === "stop" ? "유튜브 송출이 멈춥니다." + (lv.timer_repeat ? " (타이머 반복도 꺼집니다)" : "") : "비디오만 다시 띄워 1~3초 끊겼다가 같은 방송으로 돌아옵니다. 음악은 이어서 나옵니다."}`)) return;
  try { await api("POST", `/api/lives/${lv.id}/${action}`); toast(`${lv.name}: ${label} 요청`); setTimeout(softRefresh, 600); setTimeout(softRefresh, 2500); }
  catch (e) { toast(e.message, true); }
}

function openPreview(lv) {
  openModal(`미리보기 — ${lv.name}`, h("div", { style: "aspect-ratio:16/9" },
    h("iframe", { src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(lv.youtube_video_id)}?autoplay=1&mute=1`,
      style: "width:100%;height:100%;border:0;border-radius:8px", allow: "autoplay; encrypted-media", allowfullscreen: true })), null, { wide: true });
}

// 로그 파일의 시각은 서버 기준(UTC) 그대로 두고, 화면에서만 한국 시간으로 바꿔 보여 준다.
// 줄 맨 앞 시각 형식: 2026-11-03T22:32:59.763Z (관리·송출) / 2026/11/03 22:33:00 (음악) / 2026.11.03 22:33:00 (TLS)
let LOG_KST = (() => { try { return localStorage.getItem("lm.logKst") !== "0"; } catch (e) { return true; } })();
const KST = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
function logText(raw) {
  if (!LOG_KST) return raw;
  return raw.replace(/^(\d{4})[-/.](\d{2})[-/.](\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?/gm,
    (m, y, mo, d, hh, mi, ss) => KST.format(new Date(Date.UTC(+y, +mo - 1, +d, +hh, +mi, +ss))) + " KST");
}
function logPre(maxH) {
  const pre = h("pre", { class: "log", style: maxH ? `max-height:${maxH}` : "" }, "");
  pre.raw = "";
  pre.show = (raw) => { pre.raw = raw; pre.textContent = logText(raw); pre.scrollTop = pre.scrollHeight; };
  return pre;
}
function tzSeg(pre) {
  const seg = h("div", { class: "seg tz" }, [[true, "한국 시간"], [false, "UTC (원본)"]].map(([v, l]) =>
    h("button", { type: "button", class: LOG_KST === v ? "on" : "", onclick: (e) => {
      LOG_KST = v; try { localStorage.setItem("lm.logKst", v ? "1" : "0"); } catch (er) { /* 무시 */ }
      [...seg.children].forEach((b) => b.classList.toggle("on", b === e.currentTarget)); pre.show(pre.raw);
    } }, l)));
  return seg;
}

// 문제 표시(경고·중단 상태, 재연결·밀림 횟수, 관제의 빨간 칸, 알림)를 누르면 그 라이브의 로그를 바로 연다
function logClick(lv, el) {
  if (!el || !lv) return el;
  el.classList.add("logclick");
  el.title = (el.title ? el.title + "\n" : "") + "눌러서 로그 보기";
  el.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); openLog(lv); });
  return el;
}

async function openLog(lv) {
  const sel = h("div", { class: "seg" });
  const pre = logPre();
  pre.textContent = "불러오는 중…";
  const load = async (name) => {
    [...sel.children].forEach((b) => b.classList.toggle("on", b.dataset.n === name));
    try { const r = await api("GET", `/api/logs?name=${name}&lines=300`); pre.show(r.text); }
    catch (e) { pre.textContent = e.message; }
  };
  [["live", "송출"], ["audio", "음악"], ["tls", "TLS 중계"]].forEach(([p, label]) =>
    sel.append(h("button", { "data-n": `${p}-${lv.id}`, onclick: () => load(`${p}-${lv.id}`) }, label)));
  openModal(`로그 — ${lv.name}`, [h("div", { class: "logbar" }, sel, tzSeg(pre)), h("div", { style: "height:12px" }), pre], null, { wide: true });
  load(`live-${lv.id}`);
}

// ------------------------------------------------------------------ 라이브 추가/수정
async function openLiveEditor(lv) {
  const isNew = !lv;
  let media, chans;
  try {
    [media, chans] = await Promise.all([
      Promise.all([api("GET", "/api/media/video"), api("GET", "/api/media/audio")]).then(([v, a]) => ({ video: v, audio: a })),
      api("GET", "/api/channels").then((r) => r.channels)]);
  } catch (e) { toast(e.message, true); return; }
  const cur = lv || { name: "", video_folders: [], audio_folders: [], audio_mode: "random_norepeat", norepeat_count: 30, crossfade: 0, youtube_video_id: "", fast_relay: true, video_order: "sequential",
    timer_mode: "off", timer_minutes: 710, timer_until: 0, timer_repeat: false, timer_gap_minutes: 2 };
  const name = h("input", { class: "input", value: cur.name, placeholder: "예: 재즈 색소폰 24/7", maxlength: 80 });
  // 채널 → 그 채널의 스트림 키 (키는 채널 메뉴에서만 등록). 다른 라이브가 쓰는 키는 고를 수 없다
  let chId = cur.channel_id || (chans.length === 1 ? chans[0].id : ""), keyId = cur.key_id || "";
  const chSel = h("select", { class: "input", style: "max-width:320px" }, h("option", { value: "" }, "채널 선택"),
    chans.map((c) => h("option", { value: c.id }, c.name)));
  chSel.value = chId;
  const keyBox = h("div", { class: "keys" });
  const drawKeys = () => {
    const c = chans.find((x) => x.id === chId);
    if (!c) { keyBox.replaceChildren(h("div", { class: "muted small" }, chans.length ? "채널을 고르세요" : "등록된 채널이 없습니다. '채널' 메뉴에서 채널과 스트림 키를 먼저 등록하세요.")); return; }
    if (!c.keys.length) { keyBox.replaceChildren(h("div", { class: "muted small" }, "이 채널에 등록된 스트림 키가 없습니다. '채널' 메뉴에서 추가하세요.")); return; }
    keyBox.replaceChildren(...c.keys.map((k) => {
      const mine = lv && k.used_by === lv.id;
      const taken = k.used_by && !mine;
      const rb = h("input", { type: "radio", name: "key", value: k.id, disabled: taken });
      rb.checked = keyId === k.id && !taken;
      const lab = h("label", { class: "check" + (rb.checked ? " on" : "") + (taken ? " taken" : "") }, rb,
        h("span", { class: "nm" }, h("b", {}, k.label), h("span", { class: "desc" }, k.masked)),
        taken ? h("span", { class: "pill " + (k.status === "live" ? "r" : "") }, `${k.status === "live" ? "생방중" : "등록"} · ${k.used_by_name}`) : null);
      rb.addEventListener("change", () => { keyId = k.id; keyBox.querySelectorAll(".check").forEach((x) => x.classList.toggle("on", x === lab)); });
      return lab;
    }));
  };
  chSel.addEventListener("change", () => { chId = chSel.value; keyId = ""; drawKeys(); });
  drawKeys();
  const checks = (kind, selected) => {
    const box = h("div", { class: "checks " + kind });  // 음악은 하늘색 — 비디오와 구분
    if (!media[kind].folders.length) box.append(h("div", { class: "muted small" }, `${kind === "video" ? "비디오" : "음악"} 폴더가 없습니다. 먼저 '${kind === "video" ? "비디오" : "음악"}' 메뉴에서 폴더를 만들고 파일을 올리세요.`));
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
  let vorder = cur.video_order === "random" ? "random" : "sequential";
  const vorderSeg = h("div", { class: "seg" }, [["sequential", "이름순 (1, 2, … 10)"], ["random", "섞기"]].map(([v, l]) => {
    const b = h("button", { type: "button", class: v === vorder ? "on" : "", onclick: () => { vorder = v; [...vorderSeg.children].forEach((x) => x.classList.toggle("on", x === b)); } }, l);
    return b;
  }));
  // 고른 비디오 폴더들의 해상도·fps 가 섞이면 경고 (송출은 파일 그대로라 섞이면 유튜브 화면이 깨질 수 있음)
  const mixWarn = h("div", { class: "msg", hidden: true });
  const checkMix = () => {
    const sel = vbox.values();
    const fm = new Set(media.video.folders.filter((f) => sel.includes(f.name)).flatMap((f) => f.formats || []));
    mixWarn.hidden = fm.size <= 1;
    mixWarn.textContent = `해상도·프레임이 섞여 있습니다 (${[...fm].join(" / ")}). 한 라이브에서는 하나로 통일하세요 — 섞이면 유튜브 화면이 깨질 수 있습니다.`;
  };
  vbox.addEventListener("change", checkMix); checkMix();
  let mode = cur.audio_mode;
  const modeSeg = h("div", { class: "seg" }, MODES.map(([v, l]) => {
    const b = h("button", { type: "button", class: v === mode ? "on" : "", onclick: () => { mode = v; [...modeSeg.children].forEach((x) => x.classList.toggle("on", x === b)); } }, l);
    return b;
  }));
  const norep = h("input", { class: "input", type: "number", min: 0, max: 1000, value: cur.norepeat_count });
  const xfade = h("input", { class: "input", type: "number", min: 0, max: 10, step: 0.5, value: cur.crossfade });
  const vid = h("input", { class: "input", value: cur.youtube_video_id || "", placeholder: "선택 — youtu.be/ 뒤의 영상 ID" });
  // 새 중계기가 기본. 체크하면 이 라이브만 기존 방식(stunnel) — 문제 해결·비교용
  const legacy = h("input", { type: "checkbox" }); legacy.checked = cur.fast_relay === false;
  const fastLab = h("label", { class: "check" + (legacy.checked ? " on" : ""), style: "max-width:420px" }, legacy,
    h("span", { class: "nm" }, h("b", {}, "기존 송출 방식 사용 (문제 해결용)"), h("span", { class: "desc" }, "평소엔 끄세요. 이 라이브만 예전 방식(stunnel)으로 송출합니다 — 새 방식에 문제가 있을 때 비교용.")));
  legacy.addEventListener("change", () => fastLab.classList.toggle("on", legacy.checked));
  const msg = h("div", { class: "msg err" });
  const adv = h("div", { class: "adv" }, h("div", { class: "section-title", style: "margin:18px 0 12px" }, "추가 설정"),
    h("label", { class: "field" }, h("span", {}, "중복방지 곡 수"), norep),
    h("label", { class: "field" }, h("span", {}, "크로스페이드 (초)"), xfade, h("div", { class: "hint" }, "0 = 오디오 copy (CPU 최소). 0보다 크면 이 라이브만 오디오를 디코딩·재인코딩합니다.")),
    h("label", { class: "field" }, h("span", {}, "유튜브 영상 ID"), vid, h("div", { class: "hint" }, "넣으면 카드에서 유튜브 미리보기를 열 수 있습니다.")),
    h("div", { class: "field" }, h("span", { class: "flabel" }, "송출 방식"), fastLab,
      h("div", { class: "hint" }, "켜거나 끄면 이 라이브의 영상만 잠깐(2~3초) 다시 연결됩니다. 연속으로 실패하면 30분간 자동으로 기존 방식으로 송출합니다.")));

  // 타이머
  let tmode = cur.timer_mode || "off";
  const tH = h("input", { class: "input", type: "number", min: 0, max: 24, value: Math.floor(cur.timer_minutes / 60), style: "width:84px" });
  const tM = h("input", { class: "input", type: "number", min: 0, max: 59, step: 5, value: cur.timer_minutes % 60, style: "width:84px" });
  const untilDefault = cur.timer_until && cur.timer_until > Date.now() / 1000 ? cur.timer_until : Date.now() / 1000 + 710 * 60;
  const tU = h("input", { class: "input", type: "datetime-local", value: toLocalInput(untilDefault), style: "max-width:240px" });
  const tRep = h("input", { type: "checkbox" }); tRep.checked = !!cur.timer_repeat;
  const tGap = h("input", { class: "input", type: "number", min: 1, max: 120, value: cur.timer_gap_minutes, style: "width:84px" });
  const repLab = h("label", { class: "check" + (tRep.checked ? " on" : ""), style: "max-width:340px" }, tRep, h("span", { class: "nm" }, "끝나면 새 방송으로 다시 시작 (반복)"));
  tRep.addEventListener("change", () => repLab.classList.toggle("on", tRep.checked));
  const pDur = h("div", { class: "timer-pane" },
    h("div", { class: "row" }, tH, h("span", {}, "시간"), tM, h("span", {}, "분 동안 방송하고 자동 종료")),
    h("div", { class: "row", style: "margin-top:10px" }, repLab, h("span", { class: "muted small" }, "간격"), tGap, h("span", { class: "muted small" }, "분")),
    h("div", { class: "hint" }, "시작 버튼을 누른 시각부터 잽니다. 자동 재시작(감시)이 있어도 같은 방송으로 이어서 계산합니다."));
  const pUntil = h("div", { class: "timer-pane" }, h("div", { class: "row" }, tU, h("span", {}, "에 자동 종료 (내 시간 기준)")));
  const showPane = () => { pDur.hidden = tmode !== "duration"; pUntil.hidden = tmode !== "until"; tipYT.hidden = tmode === "off"; };
  const tipYT = h("div", { class: "tip" }, "유튜브 스튜디오의 스트림 설정에서 ", h("b", {}, "'자동 시작'과 '자동 중지 사용'"),
    "을 켜 두세요. 송출이 멈추면 방송이 끝나고 다시보기 영상으로 남습니다. 12시간이 넘는 방송은 다시보기가 남지 않을 수 있어 11시간 50분을 권장합니다.");
  const timerSeg = h("div", { class: "seg" }, [["off", "끄기"], ["duration", "방송 시간"], ["until", "종료 시각"]].map(([v, l]) => {
    const b = h("button", { type: "button", class: v === tmode ? "on" : "", onclick: () => { tmode = v; [...timerSeg.children].forEach((x) => x.classList.toggle("on", x === b)); showPane(); } }, l);
    return b;
  }));
  showPane();

  const payload = (apply) => ({ name: name.value, channel_id: chId, key_id: keyId, video_folders: vbox.values(), audio_folders: abox.values(),
    audio_mode: mode, norepeat_count: Number(norep.value), crossfade: Number(xfade.value), youtube_video_id: vid.value, fast_relay: !legacy.checked, video_order: vorder, apply,
    timer_mode: tmode, timer_minutes: Number(tH.value || 0) * 60 + Number(tM.value || 0),
    timer_until: tU.value ? new Date(tU.value).getTime() / 1000 : 0, timer_repeat: tRep.checked, timer_gap_minutes: Number(tGap.value || 2) });
  const save = async (apply) => {
    msg.textContent = "";
    try {
      const r = isNew ? await api("POST", "/api/lives", payload(apply)) : await api("PUT", `/api/lives/${lv.id}`, payload(apply));
      closeModal();
      toast(r.message || (isNew ? "저장했습니다 — 목록에서 '시작'을 누르면 송출이 시작됩니다" : apply !== "now" ? "저장했습니다" : isNew ? "저장하고 송출을 시작했습니다" : r.restarted ? "저장하고 적용했습니다 (잠깐 끊겼다가 복귀)" : "저장했습니다 (재시작 없이 바로 반영)"));
      softRefresh(); setTimeout(softRefresh, 2500);
    } catch (e) { msg.textContent = e.message; }
  };
  openModal(isNew ? "라이브 추가" : `라이브 설정 — ${lv.name}`, [
    h("label", { class: "field" }, h("span", {}, "이름"), name),
    h("div", { class: "field" }, h("span", { class: "flabel" }, "채널"), chSel),
    h("div", { class: "field" }, h("span", { class: "flabel" }, "스트림 키"), keyBox,
      h("div", { class: "hint" }, "키는 '채널' 메뉴에서 등록합니다. 다른 라이브가 쓰는(등록·생방중) 키는 고를 수 없습니다.")),
    h("div", { class: "field" }, h("span", { class: "flabel" }, "비디오 폴더 (여러 개 선택 가능)"), vbox, mixWarn),
    h("div", { class: "field", style: "margin-top:10px" }, h("span", { class: "flabel" }, "영상 순서"), vorderSeg,
      h("div", { class: "hint" }, "이름순: 고른 폴더 순서대로, 폴더 안은 1, 2, … 9, 10 순서로 반복 — 이름으로 순서를 짤 수 있어요.")),
    h("div", { class: "field", style: "margin-top:14px" }, h("span", { class: "flabel" }, "음악 폴더 (여러 개 선택 가능)"), abox),
    h("div", { class: "field", style: "margin-top:14px" }, h("span", { class: "flabel" }, "재생 방식"), modeSeg),
    h("div", { class: "field", style: "margin-top:18px" }, h("span", { class: "flabel" }, "타이머 (자동 종료)"), timerSeg, pDur, pUntil, tipYT),
    adv, msg,
  ], [
    isNew ? null : h("button", { class: "btn danger", onclick: async () => {
      if (!confirm(`'${lv.name}' 라이브를 삭제할까요? 송출이 멈추고 설정·로그가 지워집니다. (채널의 스트림 키는 남아 다른 라이브에서 쓸 수 있습니다)`)) return;
      try { await api("DELETE", `/api/lives/${lv.id}`); closeModal(); toast("삭제했습니다"); softRefresh(); } catch (e) { msg.textContent = e.message; }
    } }, "삭제"),
    h("span", { class: "spacer" }),
    h("button", { class: "btn ghost", onclick: closeModal }, "취소"),
    // 새 라이브는 저장만 — 송출은 목록에서 '시작'을 눌러야 시작된다
    isNew ? h("button", { class: "btn primary", onclick: () => save("later") }, "저장")
      : h("button", { class: "btn", onclick: () => save("later") }, "저장만 (다음 시작 때 적용)"),
    isNew ? null : h("button", { class: "btn primary", onclick: () => save("now") }, "저장하고 지금 적용"),
  ], { wide: true });
  setTimeout(() => name.focus(), 50);
}

// ------------------------------------------------------------------ 미디어
let mediaFolder = { video: null, audio: null };

async function showMedia(kind) {
  if (samePage(`media-${kind}`)) return PAGE.refresh();
  stopPoll();
  const label = kind === "video" ? "비디오" : "음악";
  // 폴더 목록만 갱신하고, '새 폴더 이름' 입력칸은 한 번만 만들어 그대로 둔다 (쓰는 중에 지워지지 않게)
  const listBox = h("div", {});
  const newName = h("input", { class: "input", placeholder: "새 폴더 이름", maxlength: 64 });
  const left = h("div", { class: "card" },
    h("div", { class: "card-head" }, `${label} 폴더`),
    listBox,
    h("div", { class: "card-body", style: "border-top:1px solid var(--line)" },
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        try { await api("POST", `/api/media/${kind}`, { name: newName.value }); mediaFolder[kind] = newName.value.trim(); newName.value = ""; load(); }
        catch (er) { toast(er.message, true); } } },
        newName, h("button", { class: "btn primary sm" }, "만들기")),
      h("p", { class: "hint" }, kind === "video"
        ? "규격: H.264 High, 1080p 30fps 5000k 기본 (720p~4K, 24~60fps 도 가능 — 올린 파일 그대로 송출), 키프레임 2초, B프레임·오디오 없음. PC의 변환기(LiveMax-Convert)로 만들면 됩니다."
        : "규격: AAC-LC 128k~192k 44.1kHz 스테레오 (-14 LUFS). PC의 변환기(LiveMax-Convert)로 만들면 됩니다.")));
  const right = h("div", {});
  mount(shell(kind, h("div", { class: "media-layout" }, left, right)));
  let data;
  const load = async () => {
    data = await api("GET", `/api/media/${kind}`);
    if (!mediaFolder[kind] || !data.folders.find((f) => f.name === mediaFolder[kind])) mediaFolder[kind] = data.folders[0]?.name || null;
    listBox.replaceChildren(
      h("ul", { class: "folder-list" }, data.folders.length ? data.folders.map((f) =>
        h("li", { class: f.name === mediaFolder[kind] ? "on" : "", onclick: () => { mediaFolder[kind] = f.name; load(); } },
          h("span", { class: "nm" }, f.name), f.pending ? h("span", { class: "pill y" }, `검증 ${f.pending}`) : null, h("span", { class: "ct" }, f.count)))
        : h("li", { class: "muted" }, "폴더가 없습니다")));
    await renderFolder(kind, right, data);
  };
  PAGE.name = `media-${kind}`; PAGE.refresh = load; PAGE.root = listBox;
  await load();
  pollTimer = setInterval(async () => { if (!uploading) { try { await load(); } catch (e) { /* 무시 */ } } }, 10000);
}

let uploading = 0;

async function renderFolder(kind, root, data) {
  const folder = mediaFolder[kind];
  if (!folder) { root.replaceChildren(h("div", { class: "card" }, h("div", { class: "card-body muted" }, "왼쪽에서 폴더를 만드세요."))); return; }
  const info = data.folders.find((f) => f.name === folder);
  const detail = await api("GET", `/api/media/${kind}/${encodeURIComponent(folder)}`);
  // 10초마다 확인하지만, 바뀐 게 없으면 화면을 다시 그리지 않는다 (곡이 많아도 가볍게·스크롤 그대로)
  const clipNow = kind === "video" ? (lastOverview?.lives || []).filter((l) => l.state === "running" && l.clip_folder === folder).map((l) => [l.id, l.clip]) : [];
  const sig = JSON.stringify([kind, folder, detail, info, data.rejected, clipNow]);
  if (root.dataset.sig === sig && root.firstChild) return;
  root.dataset.sig = sig;
  const input = h("input", { type: "file", multiple: true, hidden: true, accept: kind === "video" ? ".mp4,.mkv" : ".m4a,.aac" });
  const drop = h("div", { class: "drop" }, h("div", {}, h("b", {}, "파일을 여기로 끌어다 놓거나 "), h("a", { style: "color:var(--green);cursor:pointer", onclick: () => input.click() }, "선택"), input),
    h("div", { class: "small faint", style: "margin-top:6px" }, "큰 파일도 끊기면 이어서 올라갑니다 · 다른 폴더·메뉴로 옮겨도 계속 올라가요 (오른쪽 아래 업로드 창)"));
  const go = (files) => uploadFiles(kind, folder, files);
  // 드래그앤드롭 오른쪽: 구글 드라이브 링크로 올리기 (붙여넣기는 사람이 직접 — 클립보드를 몰래 읽지 않는다)
  const linkBtn = h("button", { type: "button", class: "link-btn", onclick: () => openLinkModal(kind, folder) },
    h("span", { class: "lk-ico", html: ICON.link }), h("b", {}, "구글 링크로 올리기"), h("span", {}, "빠름 · 서버가 직접 받음"));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); go(e.dataTransfer.files); });
  input.addEventListener("change", () => go(input.files));

  // 송출 상태: 음악은 지금 곡(송출중)·다음 곡, 비디오는 라이브가 쓰는 폴더 전체(송출 목록)와 지금 클립(송출중) → 삭제 잠금
  // 여러 라이브가 같이 쓰면 라이브마다 표시 (채널명 · 라이브명)
  const vidUsed = kind === "video" && info?.used_by?.length > 0;
  const chName = (id) => ((lastOverview?.channels || []).find((c) => c.id === id) || {}).name || "";
  const nowClips = {};
  (lastOverview?.lives || []).filter((l) => l.state === "running" && l.clip_folder === folder)
    .forEach((l) => (nowClips[l.clip] = nowClips[l.clip] || []).push({ live: l.name, channel: chName(l.channel_id) }));
  const who = (w) => [w.channel, w.live].filter(Boolean).join(" · ");
  const stateOf = (f) => {  // [[라벨, 색, 설명], …] — 비어 있으면 잠금 없음
    if (kind === "audio") {
      return [...(f.playing || []).map((w) => [`송출중 · ${who(w)}`, "r", `${who(w)}에서 지금 나오는 곡`]),
              ...(f.next || []).map((w) => [`다음 곡 · ${who(w)}`, "y", `${who(w)}에서 바로 다음에 나올 곡`])];
    }
    const now = (nowClips[f.name] || []).map((w) => [`송출중 · ${who(w)}`, "r", `${who(w)}에서 지금 송출 중인 클립`]);
    if (now.length) return now;
    return vidUsed ? [["송출 목록", "", "라이브가 쓰는 폴더 — 라이브 설정에서 폴더를 빼면 지울 수 있습니다"]] : [];
  };
  const pills = (st, cls) => st.map((x) => h("span", { class: `pill ${x[1]} ${cls || ""}`, title: x[2] }, x[0]));
  const delBtn = (f, st, cls) => h("button", { class: cls, html: ICON.trash, disabled: st.length > 0,
    title: st.length ? `${st[0][2]} — 지금은 지울 수 없습니다` : "삭제", onclick: () => delFile(kind, folder, f.name) });
  const files = kind === "video"
    ? h("div", { class: "files" }, detail.files.map((f) => { const st = stateOf(f); return h("div", { class: "file" + (st.length ? " locked" : "") },
        h("div", { class: "th" }, f.thumb ? cimg(`/api/thumbs/${encodeURIComponent(folder)}/${encodeURIComponent(f.name)}?v=${f.v || 0}`, null, "m") : ico("film"),
          st.length ? h("div", { class: "on-th" }, pills(st)) : null),
        h("div", { class: "meta" }, h("div", { class: "nm", title: f.name }, f.name),
          h("div", { class: "faint" }, `${fmt.dur(f.duration)} · ${fmt.bytes(f.size)}`)),
        delBtn(f, st, "btn sm danger del")); }))
    : h("table", { class: "list-table" }, h("tbody", {}, detail.files.map((f) => { const st = stateOf(f); return h("tr", { class: st.length ? "locked" : "" },
        h("td", {}, f.name, st.length ? h("span", { class: "st-pills" }, pills(st)) : null),
        h("td", { class: "r" }, fmt.dur(f.duration)), h("td", { class: "r" }, fmt.bytes(f.size)),
        h("td", { class: "r", style: "width:40px" }, delBtn(f, st, "btn sm danger"))); })));

  const inUse = info?.used_by?.length > 0;
  root.replaceChildren(...[
    h("div", { class: "card" },
      h("div", { class: "card-head" }, folder, h("span", { class: "spacer" }),
        info?.used_by?.length ? h("span", { class: "pill g", style: "text-transform:none" }, `사용 중: ${info.used_by.map(liveName).join(", ")}`) : null,
        h("button", { class: "btn sm ghost", onclick: () => renameFolder(kind, folder) }, "이름 변경"),
        h("button", { class: "btn sm danger", onclick: () => deleteFolder(kind, folder, info) }, "폴더 삭제")),
      h("div", { class: "card-body" }, h("div", { class: "drop-row" }, drop, linkBtn),
        detail.pending.length ? h("div", { class: "small", style: "margin-top:12px" }, h("span", { class: "pill y" }, "규격 검증 중"), " ", detail.pending.join(", ")) : null,
        h("div", { class: "small muted", style: "margin-top:14px" }, `${detail.files.length}개 · ${fmt.bytes(info?.size || 0)}`,
          inUse ? h("span", { class: "faint" }, kind === "video" ? " · 라이브에서 쓰는 폴더라 파일을 지울 수 없습니다 (추가는 가능)"
            : " · 라이브에서 쓰는 폴더 — '송출중'·'다음 곡'만 빼고 지울 수 있습니다") : null),
        detail.files.length ? files : h("div", { class: "muted small", style: "margin-top:10px" }, "아직 파일이 없습니다."))),
    // 최근 3일 · 최대 10개만 (오래된 건 자동으로 안 보임). X 로 하나씩, '모두 지우기'로 한 번에
    data.rejected.length ? h("div", { class: "card", style: "margin-top:20px" },
      h("div", { class: "card-head" }, "검증 실패 (최근 3일)", h("span", { class: "spacer" }),
        h("button", { class: "linkbtn", onclick: async () => { try { await api("DELETE", `/api/rejections/${kind}`); softRefresh(); } catch (e) { toast(e.message, true); } } }, "모두 지우기")),
      h("div", { class: "card-body" }, h("ul", { class: "alerts" }, data.rejected.map((r) =>
        h("li", { class: "warn" }, h("span", { class: "ic" }), h("span", {}, h("time", {}, fmt.time(r.ts)), h("b", {}, `${r.folder}/${r.name} `), r.reasons.join(" · ")),
          h("button", { class: "x", title: "지우기", onclick: async () => { try { await api("DELETE", `/api/rejections/${kind}/${r.id}`); softRefresh(); } catch (e) { toast(e.message, true); } } }, "✕")))))) : null,
  ].filter(Boolean));
}

async function delFile(kind, folder, name) {
  if (!confirm(`'${name}' 을(를) 바로 삭제할까요? 되돌릴 수 없습니다.`)) return;
  try { const r = await api("DELETE", `/api/media/${kind}/${encodeURIComponent(folder)}/${encodeURIComponent(name)}`); toast(r.message || "삭제했습니다"); softRefresh(); }
  catch (e) { toast(e.message, true); }
}
async function renameFolder(kind, folder) {
  const n = prompt("새 폴더 이름", folder); if (!n || n === folder) return;
  try { await api("PUT", `/api/media/${kind}/${encodeURIComponent(folder)}`, { name: n }); mediaFolder[kind] = n.trim(); softRefresh(); } catch (e) { toast(e.message, true); }
}
async function deleteFolder(kind, folder, info) {
  const label = kind === "video" ? "비디오" : "음악";
  const typed = h("input", { class: "input", placeholder: folder, autocomplete: "off" });
  const msg = h("div", { class: "msg err" });
  const go = h("button", { class: "btn danger solid", disabled: true, onclick: async () => {
    msg.textContent = "";
    try {
      const r = await api("DELETE", `/api/media/${kind}/${encodeURIComponent(folder)}?confirm=${encodeURIComponent(typed.value)}`);
      closeModal(); toast(r.message); mediaFolder[kind] = null; softRefresh();
    } catch (e) { msg.textContent = e.message; }
  } }, "영구 삭제");
  typed.addEventListener("input", () => { go.disabled = typed.value !== folder; });
  openModal(`${label} 폴더 삭제`, [
    h("div", { class: "danger-box" },
      h("b", {}, `'${folder}' 폴더와 안의 ${label} 파일 ${info?.count ?? 0}개 (${fmt.bytes(info?.size || 0)})를 모두 지웁니다.`),
      h("ul", {}, h("li", {}, "서버에서 바로 영구 삭제됩니다. 휴지통이 없어 되돌릴 수 없습니다."),
        h("li", {}, "썸네일·업로드 대기·격리 파일·기록까지 함께 지워 아무것도 남기지 않습니다."),
        h("li", {}, "PC에 원본이 없다면 먼저 내려받아 두세요."))),
    h("label", { class: "field", style: "margin-top:16px" }, h("span", {}, `확인을 위해 폴더 이름 '${folder}' 을(를) 그대로 입력하세요`), typed), msg,
  ], [h("span", { class: "spacer" }), h("button", { class: "btn ghost", onclick: closeModal }, "취소"), go]);
  setTimeout(() => typed.focus(), 50);
}

// 이어 올리기: 8MB 조각, 끊기면 서버가 가진 지점부터 다시.
// 업로드는 화면과 따로 돈다: 폴더·메뉴를 옮겨도 계속 올라가고, 오른쪽 아래 '업로드' 창에서 모든 폴더 것을 함께 본다.
// 여러 폴더에 끌어다 놓아도 한 줄로 세워 한 번에 한 파일씩 (서버 부담을 고르게 — 속도 상한은 서버가 건다).
const CHUNK = 8 * 1024 * 1024;
const UP = { queue: [], items: [], running: false, panel: null, body: null, head: null, min: false };
const KIND_LABEL = { video: "비디오", audio: "음악" };

function upPanel() {
  if (UP.panel) return UP.panel;
  UP.head = h("div", { class: "up-head" });
  UP.body = h("div", { class: "up-body" });
  UP.panel = h("div", { class: "up-panel" }, UP.head, UP.body);
  document.body.append(UP.panel);
  return UP.panel;
}
function upHead() {
  const left = UP.queue.length + (UP.running ? 1 : 0) + UP.items.filter((x) => x.fetch && (x.state === "wait" || x.state === "run")).length;
  const done = UP.items.filter((x) => x.state === "done").length, fail = UP.items.filter((x) => x.state === "err").length;
  const canceled = UP.items.filter((x) => x.state === "cancel").length;
  UP.head.replaceChildren(
    h("b", {}, left ? `업로드 중 · 남은 ${left}개` : `업로드 끝 · 완료 ${done}${fail ? ` · 실패 ${fail}` : ""}${canceled ? ` · 취소 ${canceled}` : ""}`),
    h("span", { class: "spacer" }),
    left ? h("button", { class: "btn sm danger", onclick: () => { if (confirm(`남은 ${left}개 업로드를 모두 취소할까요?`)) upCancelAll(); } }, "모두 취소")
      : h("button", { class: "btn sm", onclick: () => { UP.items = []; UP.groups = {}; UP.panel.remove(); UP.panel = null; } }, "닫기"),
    h("button", { class: "btn sm", title: UP.min ? "펼치기" : "접기", onclick: () => { UP.min = !UP.min; UP.panel.classList.toggle("min", UP.min); upHead(); } }, UP.min ? "▲" : "▼"));
  for (const g of Object.values(UP.groups || {})) upGroupHead(g);
}
// 폴더별 묶음: [음악 · 재즈] 2/5 ─ 파일들
function upGroup(kind, folder) {
  UP.groups = UP.groups || {};
  const key = `${kind}/${folder}`;
  if (!UP.groups[key]) {
    const g = { kind, folder, head: h("div", { class: "up-ghead" }), list: h("div", { class: "up-glist" }) };
    g.el = h("div", { class: "up-group" }, g.head, g.list);
    UP.groups[key] = g; UP.body.append(g.el);
  }
  return UP.groups[key];
}
function upGroupHead(g) {
  const its = UP.items.filter((x) => x.kind === g.kind && x.folder === g.folder);
  const fin = its.filter((x) => ["done", "err", "cancel"].includes(x.state)).length;
  g.head.replaceChildren(h("span", {}, `${KIND_LABEL[g.kind]} · ${g.folder}`), h("span", { class: "faint" }, `${fin}/${its.length}`));
}
function upRow(it) {
  it.bar = h("i", { style: "width:0%" });
  it.txt = h("span", { class: "st" }, "대기");
  it.x = h("button", { class: "up-x", title: "이 파일 업로드 취소", onclick: () => upCancel(it) }, "✕");
  it.row = h("div", { class: "up" },
    h("div", { class: "row" }, h("span", { class: "nm", title: it.file.name }, it.file.name), it.txt, it.x),
    h("div", { class: "bar" }, it.bar));
  return it.row;
}
function upCancel(it) {
  if (it.fetch) {  // 링크로 가져오기: 서버에 취소 요청 (상태는 다음 확인 때 반영)
    if (it.state === "wait" || it.state === "run") { it.txt.textContent = "취소 중…"; api("DELETE", `/api/fetch/${it.id}`).catch(() => {}); }
    return;
  }
  if (it.state === "wait") {
    UP.queue = UP.queue.filter((x) => x !== it);
    upMark(it, "cancel", "취소됨");
  } else if (it.state === "run") {
    it.cancel = true; it.txt.textContent = "취소 중…";  // 지금 보내는 조각(최대 8MB)이 끝나면 멈추고 서버의 조각도 지운다
  }
  upHead();
}
function upCancelAll() {
  for (const it of [...UP.queue]) upCancel(it);
  for (const it of UP.items.filter((x) => x.state === "run" || (x.fetch && x.state === "wait"))) upCancel(it);
}

function openLinkModal(kind, folder) {
  const links = h("textarea", { class: "input", rows: 5, placeholder: "https://drive.google.com/file/d/..../view?usp=sharing\n(여러 개면 한 줄에 하나씩)" });
  const msg = h("div", { class: "msg err" });
  const go = async () => {
    msg.textContent = "";
    try { await fetchLinks(kind, folder, links.value); closeModal(); toast("서버가 받기 시작했습니다 — 오른쪽 아래 창에서 확인"); }
    catch (e) { msg.textContent = e.message; }
  };
  // Enter = 가져오기 (줄바꿈은 Shift+Enter) — 이 창에서만
  links.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); } });
  openModal(`구글 링크로 올리기 — ${KIND_LABEL[kind]} · ${folder}`, [
    h("label", { class: "field" }, h("span", {}, "공유 링크 (복사해 둔 링크가 있으면 자동으로 들어갑니다 · Enter = 가져오기)"), links),
    h("div", { class: "tip" }, h("b", {}, "구글 드라이브: "), "파일 오른쪽 클릭 → 공유 → 일반 액세스를 ", h("b", {}, "'링크가 있는 모든 사용자'"),
      "로 바꾸고 → 링크 복사. 폴더 링크는 안 되고 파일마다 넣으세요. 드롭박스 공유 링크와 일반 다운로드 주소도 됩니다."),
    msg],
    [h("button", { class: "btn ghost", onclick: closeModal }, "취소"), h("button", { class: "btn primary", onclick: go }, "가져오기")]);
  setTimeout(() => links.focus(), 50);
  // 복사해 둔 링크가 있으면 미리 넣어 둔다 (이 버튼을 눌렀을 때만 읽음 — 링크 모양일 때만, 처음엔 브라우저가 허용을 물어봄)
  if (navigator.clipboard?.readText) {
    navigator.clipboard.readText().then((t) => {
      const urls = (t || "").split(/\s+/).filter((x) => /^https?:\/\//i.test(x));
      if (urls.length && !links.value) { links.value = urls.join("\n"); links.select(); }
    }).catch(() => { /* 허용 안 함·지원 안 함 → 직접 붙여넣기 */ });
  }
}

// 다 올린 뒤: 서버의 규격 검사가 끝날 때까지 지켜보고 결과로 바꾼다 (등록됨 / 검증 실패)
const VERIFY = { items: new Set(), timer: null };
function upVerify(it) {
  it.verifyStart = Date.now();
  VERIFY.items.add(it);
  if (!VERIFY.timer) VERIFY.timer = setInterval(upVerifyPoll, 3000);
}
async function upVerifyPoll() {
  const groups = {};
  for (const it of VERIFY.items) (groups[`${it.kind}/${it.folder}`] = groups[`${it.kind}/${it.folder}`] || []).push(it);
  for (const its of Object.values(groups)) {
    const { kind, folder } = its[0];
    let d;
    try { d = await api("GET", `/api/media/${kind}/${encodeURIComponent(folder)}`); } catch (e) { continue; }
    const have = new Set(d.files.map((f) => f.name)), wait = new Set(d.pending || []);
    for (const it of its) {
      const name = it.file.name.normalize("NFC").trim();  // 서버는 맥(NFD) 한글 이름을 NFC 로 합쳐 저장한다
      if (have.has(name)) { it.txt.textContent = "등록 완료 ✓"; VERIFY.items.delete(it); }
      else if (!wait.has(name) && Date.now() - it.verifyStart > 4000) {
        it.txt.textContent = "검증 실패 — 폴더 화면 '검증 실패' 목록 확인"; it.row.classList.remove("ok"); it.row.classList.add("err"); VERIFY.items.delete(it);
      } else if (Date.now() - it.verifyStart > 180000) { it.txt.textContent = "검증이 오래 걸림 — 폴더 화면에서 확인"; VERIFY.items.delete(it); }
    }
  }
  if (!VERIFY.items.size) { clearInterval(VERIFY.timer); VERIFY.timer = null; softRefreshMedia(); }
}
function softRefreshMedia() { if (/^#\/media\//.test(location.hash)) softRefresh(); }

// 링크로 가져오기: 서버가 받는 상황을 1.5초마다 받아 업로드 창에 같이 보여 준다
async function fetchLinks(kind, folder, text) {
  const r = await api("POST", `/api/media/${kind}/${encodeURIComponent(folder)}/fetch`, { urls: text });
  upPanel();
  const g = upGroup(kind, folder);
  for (const j of r.jobs) {
    const it = { fetch: true, id: j.id, kind, folder, state: "wait", file: { name: `링크 · ${j.source}` } };
    UP.items.push(it); g.list.append(upRow(it));
    it.txt.textContent = "대기 (서버가 받음)";
  }
  upHead();
  if (!UP.fetchTimer) UP.fetchTimer = setInterval(fetchPoll, 1500);
}
async function fetchPoll() {
  let jobs;
  try { jobs = (await api("GET", "/api/fetch")).jobs; } catch (e) { return; }
  const byId = Object.fromEntries(jobs.map((j) => [j.id, j]));
  let active = 0;
  for (const it of UP.items.filter((x) => x.fetch && (x.state === "wait" || x.state === "run"))) {
    const j = byId[it.id];
    if (!j) { upMark(it, "err", "기록 없음 (서버 재시작?)"); continue; }
    if (j.name) it.row.querySelector(".nm").textContent = j.name;
    if (j.state === "run") {
      it.state = "run";
      const pct = j.total ? (j.got / j.total) * 100 : 0;
      it.bar.style.width = (j.total ? pct : 30).toFixed(1) + "%";
      it.txt.textContent = `${j.total ? pct.toFixed(0) + "% · " : fmt.bytes(j.got) + " · "}${fmt.bytes(j.speed)}/s`;
      active++;
    } else if (j.state === "wait") { active++; }
    else if (j.state === "done") { it.bar.style.width = "100%"; upMark(it, "done", "받기 완료 · 검증 대기"); it.file = { name: j.name }; upVerify(it); }
    else if (j.state === "cancel") upMark(it, "cancel", "취소됨");
    else if (j.state === "err") upMark(it, "err", "실패: " + j.error);
  }
  upHead();
  if (!active) {
    clearInterval(UP.fetchTimer); UP.fetchTimer = null;
    setTimeout(() => { const m = location.hash.match(/^#\/media\/(video|audio)/); if (m && !UP.running) showMedia(m[1]); }, 4000);
  }
}
function upMark(it, state, text) {
  it.state = state; it.txt.textContent = text;
  it.row.classList.add(state === "done" ? "ok" : state === "err" ? "err" : "cancel");
  it.x.remove();
}

function uploadFiles(kind, folder, fileList) {
  const ext = kind === "video" ? [".mp4", ".mkv"] : [".m4a", ".aac"];
  const files = [...fileList].filter((f) => {
    if (ext.some((e) => f.name.toLowerCase().endsWith(e))) return true;
    toast(`${f.name}: ${ext.join(", ")} 파일만 올릴 수 있습니다`, true); return false;
  });
  if (!files.length) return;
  upPanel();
  const g = upGroup(kind, folder);
  for (const file of files) {
    const it = { file, kind, folder, state: "wait" };
    UP.items.push(it); UP.queue.push(it);
    g.list.append(upRow(it));
  }
  upHead();
  if (!UP.running) upWorker();
}

async function upWorker() {
  UP.running = true;
  while (UP.queue.length) {
    const it = UP.queue.shift();
    upHead();
    await upOne(it);
    upHead();
    // 지금 보고 있는 곳이 그 폴더면 목록만 새로 (다른 메뉴로 끌고 가지 않는다)
    if (it.state === "done" && location.hash.startsWith(`#/media/${it.kind}`) && mediaFolder[it.kind] === it.folder && !UP.queue.length) {
      setTimeout(() => { if (location.hash.startsWith(`#/media/${it.kind}`) && !UP.running) showMedia(it.kind); }, 4000);
    }
  }
  UP.running = false;
  upHead();
}

async function upOne(it) {
  const { file, kind, folder } = it;
  it.state = "run"; uploading++;
  let id = null;
  try {
    let offset;
    ({ id, offset } = await api("POST", "/api/uploads", { kind, folder, name: file.name, size: file.size }));
    let fails = 0; const t0 = Date.now(); const start = offset;
    while (offset < file.size) {
      if (it.cancel) break;
      try {
        const r = await api("PUT", `/api/uploads/${id}?offset=${offset}`, file.slice(offset, offset + CHUNK));
        offset = r.offset; fails = 0;
        const pct = (offset / file.size) * 100, sec = (Date.now() - t0) / 1000;
        it.bar.style.width = pct.toFixed(1) + "%";
        if (!it.cancel) it.txt.textContent = `${pct.toFixed(0)}% · ${fmt.bytes((offset - start) / Math.max(sec, 0.1))}/s`;
      } catch (e) {
        if (it.cancel) break;
        if (++fails > 8) throw e;
        it.txt.textContent = `연결 끊김 — ${fails}회째 다시 시도`;
        await new Promise((r) => setTimeout(r, Math.min(30000, 1000 * 2 ** fails)));
        const again = await api("POST", "/api/uploads", { kind, folder, name: file.name, size: file.size });
        offset = again.offset;
      }
    }
    if (it.cancel) {
      try { await api("DELETE", `/api/uploads/${id}`); } catch (e) { /* 남은 조각은 자동 정리가 지운다 */ }
      upMark(it, "cancel", "취소됨");
      return;
    }
    await api("POST", `/api/uploads/${id}/complete`);
    it.bar.style.width = "100%"; upMark(it, "done", "올리기 완료 · 검증 대기"); upVerify(it);
  } catch (e) { upMark(it, "err", "실패: " + e.message); }
  finally { uploading--; }
}

// 올리는 중에 창을 닫거나 새로고침하면 끊긴다 → 한 번 묻는다 (끊겨도 다음에 같은 파일을 올리면 이어서 올라감)
window.addEventListener("beforeunload", (e) => { if (UP.running || UP.queue.length) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ 채널
async function showChannels() {
  stopPoll();
  let list;
  try { list = (await api("GET", "/api/channels")).channels; } catch (e) { toast(e.message, true); return; }
  const STAT = { free: ["사용 가능", "g"], registered: ["등록", ""], live: ["생방중", "r"] };
  const cards = list.map((c) => {
    const label = h("input", { class: "input", placeholder: "키 이름 (예: 메인 방송)", maxlength: 30, style: "flex:1;min-width:140px" });
    const value = h("input", { class: "input", type: "password", autocomplete: "new-password", placeholder: "유튜브 스튜디오의 스트림 키", style: "flex:2;min-width:180px" });
    const addKey = async (e) => {
      e.preventDefault();
      try { await api("POST", `/api/channels/${c.id}/keys`, { label: label.value, key: value.value }); value.value = ""; toast("스트림 키를 등록했습니다"); showChannels(); }
      catch (er) { toast(er.message, true); }
    };
    return h("article", { class: "card ch-card" },
      h("div", { class: "card-head" }, chLink(c, "ch-name", chLogo(c, 44), h("span", {}, c.name)), h("span", { class: "spacer" }),
        h("button", { class: "btn sm ghost", onclick: () => openChannelEditor(c) }, "수정"),
        h("button", { class: "btn sm danger", onclick: () => deleteChannel(c) }, "삭제")),
      h("div", { class: "card-body" },
        c.description ? h("p", { class: "muted", style: "margin:0 0 6px" }, c.description) : null,
        safeUrl(c.url) ? h("a", { class: "small link", href: c.url, target: "_blank", rel: "noopener noreferrer" }, c.url) : h("span", { class: "small faint" }, "채널 주소 없음"),
        h("div", { class: "section-title", style: "margin:18px 0 8px" }, `스트림 키 ${c.keys.length}개`),
        c.keys.length ? h("table", { class: "list-table", style: "margin-top:0" }, h("tbody", {}, c.keys.map((k) => h("tr", {},
          h("td", {}, h("b", {}, k.label), h("div", { class: "small faint mono" }, k.masked)),
          h("td", {}, h("span", { class: "pill " + STAT[k.status][1] }, STAT[k.status][0]), k.used_by_name ? h("span", { class: "small muted" }, ` ${k.used_by_name}`) : null),
          h("td", { class: "r", style: "width:40px" }, h("button", { class: "btn sm danger", html: ICON.trash, title: k.used_by ? "라이브가 쓰는 키는 지울 수 없습니다" : "키 삭제",
            disabled: !!k.used_by, onclick: async () => {
              if (!confirm(`'${k.label}' 스트림 키를 지울까요?`)) return;
              try { await api("DELETE", `/api/channels/${c.id}/keys/${k.id}`); showChannels(); } catch (e) { toast(e.message, true); }
            } }))))))
          : h("div", { class: "muted small" }, "아직 등록된 키가 없습니다."),
        h("form", { class: "row", style: "margin-top:12px", onsubmit: addKey }, label, value, h("button", { class: "btn primary sm" }, "키 등록")),
        h("p", { class: "hint" }, "키는 서버의 보호된 파일에만 저장되고, 화면에는 마지막 4자리만 보입니다. 키를 바꾸려면 새 키를 등록하고 라이브에서 고른 뒤 옛 키를 지우세요.")));
  });
  mount(shell("channels", h("div", {},
    h("div", { class: "row", style: "margin-bottom:16px" }, h("div", { class: "muted" }, "채널마다 이름·로고·주소와 스트림 키를 등록하면, 라이브를 만들 때 채널과 키를 고르기만 하면 됩니다."),
      h("span", { class: "spacer" }), h("button", { class: "btn primary", onclick: () => openChannelEditor(null) }, "+ 채널 추가")),
    list.length ? h("div", { class: "grid", style: "grid-template-columns:repeat(auto-fill,minmax(min(440px,100%),1fr))" }, cards)
      : h("div", { class: "card empty" }, h("div", { class: "card-body" }, h("b", {}, "등록된 채널이 없습니다"), h("p", { class: "muted" }, "'채널 추가'로 시작하세요."))))));
}

function openChannelEditor(c) {
  const isNew = !c;
  const name = h("input", { class: "input", value: c?.name || "", maxlength: 40, placeholder: "예: Jazz Night Radio" });
  const desc = h("input", { class: "input", value: c?.description || "", maxlength: 120, placeholder: "라이브 목록의 채널 줄 오른쪽에 보입니다" });
  const url = h("input", { class: "input", value: c?.url || "", maxlength: 300, placeholder: "https://www.youtube.com/@내채널" });
  const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp", hidden: true });
  const prev = h("div", { class: "logo-prev" }, c?.logo ? h("img", { src: c.logo, alt: "" }) : h("span", { class: "faint small" }, "로고 없음"));
  let logoBlob = null, removeLogo = false;
  const takeLogo = (f) => {
    if (!f) return;
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { toast("PNG, JPG, WEBP 이미지만 됩니다", true); return; }
    if (f.size > 1000000) { toast("로고는 1MB 이하로 올려 주세요", true); return; }
    logoBlob = f; removeLogo = false;
    prev.replaceChildren(h("img", { src: URL.createObjectURL(f), alt: "" }));
  };
  file.addEventListener("change", () => takeLogo(file.files[0]));
  // 로고 칸에 이미지를 끌어다 놓아도 된다
  const logoDrop = h("div", { class: "logo-drop" }, prev,
    h("div", { class: "small muted" }, "이미지를 여기로 끌어다 놓거나 ", h("a", { class: "link", style: "cursor:pointer", onclick: () => file.click() }, "선택")),
    c?.logo ? h("button", { type: "button", class: "btn sm ghost", onclick: () => { removeLogo = true; logoBlob = null; prev.replaceChildren(h("span", { class: "faint small" }, "로고 없음")); } }, "로고 빼기") : null, file);
  logoDrop.addEventListener("dragover", (e) => { e.preventDefault(); logoDrop.classList.add("over"); });
  logoDrop.addEventListener("dragleave", () => logoDrop.classList.remove("over"));
  logoDrop.addEventListener("drop", (e) => { e.preventDefault(); logoDrop.classList.remove("over"); takeLogo(e.dataTransfer.files[0]); });
  const msg = h("div", { class: "msg err" });
  openModal(isNew ? "채널 추가" : `채널 수정 — ${c.name}`, [
    h("label", { class: "field" }, h("span", {}, "채널명"), name),
    h("label", { class: "field" }, h("span", {}, "채널 설명"), desc),
    h("label", { class: "field" }, h("span", {}, "채널 주소"), url, h("div", { class: "hint" }, "라이브 목록·썸네일의 채널명을 누르면 이 주소가 새 창으로 열립니다.")),
    h("div", { class: "field" }, h("span", { class: "flabel" }, "채널 로고 (정사각형 PNG/JPG/WEBP, 1MB 이하)"), logoDrop),
    msg,
  ], [h("span", { class: "spacer" }), h("button", { class: "btn ghost", onclick: closeModal }, "취소"),
    h("button", { class: "btn primary", onclick: async () => {
      msg.textContent = "";
      try {
        const body = { name: name.value, description: desc.value, url: url.value };
        const id = isNew ? (await api("POST", "/api/channels", body)).id : (await api("PUT", `/api/channels/${c.id}`, body), c.id);
        if (logoBlob) await api("POST", `/api/channels/${id}/logo`, logoBlob);
        else if (removeLogo) await api("DELETE", `/api/channels/${id}/logo`);
        closeModal(); toast(isNew ? "채널을 추가했습니다 — 이제 스트림 키를 등록하세요" : "저장했습니다"); showChannels();
      } catch (e) { msg.textContent = e.message; }
    } }, "저장")]);
  setTimeout(() => name.focus(), 50);
}

async function deleteChannel(c) {
  if (c.lives.length) { toast(`이 채널의 라이브 ${c.lives.length}개를 먼저 지우거나 다른 채널로 옮기세요`, true); return; }
  if (!confirm(`'${c.name}' 채널을 삭제할까요?\n등록된 스트림 키 ${c.keys.length}개와 로고도 함께 지워집니다.`)) return;
  try { await api("DELETE", `/api/channels/${c.id}`); toast("채널을 삭제했습니다"); showChannels(); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ 설정 / 시스템
let sysRetried = false;
async function showSystem() {
  stopPoll();
  let s;
  try { s = await api("GET", "/api/system"); } catch (e) { return; }
  if (!s.update_check?.checked_at && !sysRetried) {  // 처음 열 때 새 버전 확인 결과가 아직 없으면 잠시 뒤 한 번 다시
    sysRetried = true;
    setTimeout(() => { if (location.hash.startsWith("#/system")) showSystem(); }, 5000);
  }
  const [bh, bm] = s.settings.restart_base_utc.split(":").map(Number);
  const local = new Date(Date.UTC(2000, 0, 1, bh, bm));
  const base = h("input", { class: "input", type: "time", value: `${pad(local.getHours())}:${pad(local.getMinutes())}`, style: "max-width:160px" });
  const relay = h("input", { type: "checkbox" }); relay.checked = s.settings.tls_relay;
  relay.addEventListener("change", () => {
    if (relay.checked) return;
    const ok = confirm("⚠ TLS 중계를 끄면 안 됩니다 (권장: 켜 둠)\n\n" +
      "• 서버 송신 트래픽이 라이브마다 약 30~50% 늘어납니다 (5.2 → 약 8 Mbps).\n" +
      "• 라이브 15개 기준 월 약 25TB → 39TB 로, 목표 28TB·한계 32TB 를 크게 넘습니다.\n" +
      "• 저장 후 각 라이브가 다음 재시작 때부터 직접 암호화 방식으로 바뀝니다.\n\n" +
      "문제 해결용으로 잠깐 끄는 경우가 아니라면 '취소'를 누르세요.\n정말 끌까요?");
    if (!ok) relay.checked = true;
  });
  // 라이브 목록은 이 화면에서 직접 받는다 (라이브 화면을 거치지 않고 설정으로 바로 와도 로그 목록이 다 보이게)
  if (!lastOverview) { try { lastOverview = await api("GET", "/api/overview"); } catch (e) { /* 관리 프로그램 로그만이라도 */ } }
  const logSel = h("select", { class: "input", style: "max-width:260px" }, h("option", { value: "livemaxd" }, "관리 프로그램 (livemaxd)"),
    (lastOverview?.lives || []).flatMap((l) => [["live", "송출"], ["audio", "음악"], ["tls", "TLS"]].map(([p, n]) => h("option", { value: `${p}-${l.id}` }, `${l.name} — ${n}`))));
  const pre = logPre("40vh");
  const card = (title, ...body) => h("div", { class: "card", style: "margin-bottom:20px" }, h("div", { class: "card-head" }, title), h("div", { class: "card-body" }, body));
  const imp = h("input", { type: "file", accept: ".zip", hidden: true });
  imp.addEventListener("change", async () => {
    if (!imp.files[0]) return;
    try { const r = await api("POST", "/api/system/import", imp.files[0]); toast(r.message); } catch (e) { toast(e.message, true); }
  });
  mount(shell("system", h("div", { class: "grid", style: "grid-template-columns:repeat(auto-fill,minmax(min(420px,100%),1fr))" },
    h("div", {},
      serverCard(s, card),
      card("업데이트",
        h("p", {}, "현재 버전: ", h("b", { class: "mono" }, (s.version || "").slice(0, 7))),
        // 지금 GitHub 에 올라와 있는 최신 이미지와 비교한 결과 (30분마다 + 이 화면을 열 때 확인)
        ({ latest: h("p", { class: "upd ok" }, "✓ 최신 버전입니다"),
           available: h("p", { class: "upd new" }, "새 버전이 있습니다", s.update_check.latest_short ? h("b", { class: "mono" }, " " + s.update_check.latest_short) : null, " — 아래 버튼을 누르세요"),
           error: h("p", { class: "small muted" }, "새 버전 확인 실패 (잠시 뒤 다시 확인합니다)"),
         })[s.update_check?.status] || null,
        s.update_check?.checked_at ? h("p", { class: "small faint", style: "margin-top:-6px" }, `확인 ${fmt.time(s.update_check.checked_at)}`) : null,
        s.update_requested ? h("p", { class: "msg" }, "업데이트 요청됨 — 1~2분 안에 처리됩니다.")
          : s.update_last ? h("p", { class: "small " + (s.update_last.result === "failed" ? "msg err" : "muted") }, `마지막 업데이트 실행 ${fmt.time(Date.parse(s.update_last.at) / 1000)} — `,
            { latest: "그때 이미 최신이었음", updated: "새 버전으로 업데이트함", failed: "실패 (서버의 logs/update.log 확인)", running: "진행 중" }[s.update_last.result]) : null,
        // 실수 방지: 라이브 정지·재시작 버튼처럼 4초 안에 두 번 눌러야 확인 창이 뜬다 (전체 라이브가 다시 켜지는 버튼이라)
        armAny("system:update", "btn primary", "최신 버전으로 업데이트", async () => {
          if (!confirm("새 버전으로 업데이트할까요? 모든 라이브가 잠깐 끊겼다가 차례대로 복귀합니다.")) return;
          try { const r = await api("POST", "/api/system/update"); toast(r.message); showSystem(); } catch (e) { toast(e.message, true); }
        })),
      s.reboot_required ? card("서버 재부팅 필요",
        h("p", {}, "보안 업데이트(커널 등)가 설치되어 재부팅을 기다리고 있습니다. 송출 영향이 적은 시간에 서버에서 ", h("code", {}, "sudo reboot"), " 하세요."),
        s.reboot_packages.length ? h("p", { class: "small muted" }, s.reboot_packages.join(", ")) : null) : null,
      card("송출 설정",
        h("label", { class: "field" }, h("span", {}, "일일 자동 재시작 시작 시각 (내 시간 기준)"), base,
          h("div", { class: "hint" }, "라이브마다 이 시각부터 2분 간격으로 하루 1번 비디오만 재시작합니다 (1~3초 끊김). 타이머를 켠 라이브는 건너뜁니다.")),
        h("label", { class: "check", style: "margin-bottom:14px" }, relay, h("span", { class: "nm" }, "TLS 중계 사용 — 꼭 켜 두세요 (송출 트래픽 약 30% 절감)")),
        h("button", { class: "btn primary", onclick: async () => {
          const [lh, lm] = base.value.split(":").map(Number); const d = new Date(2000, 0, 1, lh, lm);
          try { await api("PUT", "/api/system/settings", { restart_base_utc: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`, tls_relay: relay.checked }); toast("저장했습니다 (각 라이브의 다음 재시작 때 적용)"); }
          catch (e) { toast(e.message, true); }
        } }, "저장")),
      card("디스크",
        h("table", { class: "list-table", style: "margin-top:0" }, h("tbody", {}, s.disk.map((d) => h("tr", {}, h("td", {}, d.label), h("td", { class: "r" }, fmt.bytes(d.bytes)))))),
        h("button", { class: "btn", style: "margin-top:12px", onclick: async () => { const r = await api("POST", "/api/system/cleanup"); toast(r.message); } }, "지금 정리 실행"),
        h("p", { class: "hint" }, "임시 파일·지난 목록·오래된 로그만 지웁니다. 올린 비디오·음악 원본은 건드리지 않습니다.")),
      card("설정 백업",
        h("p", { class: "small muted" }, "라이브 설정을 내려받아 다른 서버에 그대로 옮길 수 있습니다. 스트림 키는 포함되지 않습니다."),
        h("div", { class: "row" }, h("a", { class: "btn", href: "/api/system/export" }, "내보내기"), h("button", { class: "btn", onclick: () => imp.click() }, "가져오기"), imp))),
    h("div", {},
      notifyCard(s.notify, card),
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
        h("div", { class: "logbar" }, logSel, h("button", { class: "btn sm", onclick: async () => { const r = await api("GET", `/api/logs?name=${logSel.value}&lines=300`); pre.show(r.text); } }, "보기"), tzSeg(pre)),
        h("div", { style: "height:12px" }), pre))),
    // 로그아웃은 거의 안 쓰므로 설정 맨 아래에. 이 서버(이 주소)의 이 기기만 로그아웃된다.
    h("div", { class: "logout-row" },
      h("button", { class: "btn", onclick: logout }, h("span", { class: "ico", html: ICON.out }), "이 기기에서 로그아웃"),
      h("span", { class: "small faint" }, "이 서버에서만 로그아웃됩니다. 다른 서버의 로그인은 그대로예요."))));
}

function serverCard(s, card) {
  const name = h("input", { class: "input", value: s.settings.server_name || "", maxlength: 30, placeholder: "예: LIVE 1", style: "max-width:260px" });
  const maxLives = h("input", { class: "input", type: "number", min: 1, max: 100, value: s.settings.max_lives ?? 50, style: "max-width:120px" });
  const port = h("input", { class: "input", type: "number", min: 10, max: 100000, value: s.settings.port_mbit ?? 1000, style: "max-width:120px" });
  const traffic = h("input", { class: "input", type: "number", min: 0, max: 10000, step: "any", value: s.settings.monthly_traffic_tb ?? 0, style: "max-width:120px" });
  // 안전선: 용량 계산("N개 더 추가 가능")과 서버 박스 막대의 세로 눈금 기준
  const safe = (label, unit, v, min, max, def) => {
    const el = h("input", { class: "input", type: "number", min, max, value: v ?? def });
    return [el, h("label", { class: "safe-f" }, h("span", {}, label), h("div", { class: "safe-in" }, el, h("i", {}, unit)), h("em", {}, `기본 ${def}${unit}`))];
  };
  const [memR, memF] = safe("메모리 남길 여유", "MB", s.settings.mem_reserve_mb, 100, 100000, 700);
  const [cpuT, cpuF] = safe("CPU 평균", "%", s.settings.cpu_target_pct, 10, 95, 50);
  const [portU, portF] = safe("회선 사용", "%", s.settings.port_use_pct, 10, 100, 50);
  const [diskW, diskF] = safe("디스크", "%", s.settings.disk_warn_pct, 50, 99, 85);
  // 음악 프로그램 채널별 묶음 (메모리 절약) — LiveMax 가 다시 켜질 때 적용
  const shared = h("input", { type: "checkbox" }); shared.checked = !!s.settings.audio_shared;
  const sharedLab = h("label", { class: "check" + (shared.checked ? " on" : "") }, shared,
    h("span", { class: "nm" }, h("b", {}, "음악 프로그램을 채널별로 묶기 (메모리 절약)"),
      h("span", { class: "desc" }, "라이브마다 하나씩 띄우던 음악 프로그램을 채널마다 하나로 — 라이브 10개 기준 약 835MB → 150MB. ",
"라이브를 추가·삭제해도 다른 라이브 음악은 끊기지 않습니다. 프로그램이 멈추면 바로 다시 켜지고 송출도 몇 초 뒤 스스로 이어집니다.")));
  shared.addEventListener("change", () => sharedLab.classList.toggle("on", shared.checked));
  const pendingShared = !!s.settings.audio_shared !== !!s.settings.audio_shared_active;
  const groupMax = h("input", { class: "input", type: "number", min: 1, max: 50, value: s.settings.audio_group_max ?? 10, style: "max-width:90px" });
  return card("서버",
    h("label", { class: "field" }, h("span", {}, "서버명"), name,
      h("div", { class: "hint" }, "로고 옆과 브라우저 탭 제목(LiveMax LIVE 1), 휴대폰 알림 제목에 붙어 서버를 구분합니다.")),
    h("label", { class: "field" }, h("span", {}, "라이브 최대 개수"), maxLives,
      h("div", { class: "hint" }, "안전장치 — 이 서버에 이 개수보다 많은 라이브를 만들 수 없습니다 (기본 50 — 운영 표준). 서버 사양을 올리면 늘리세요.")),
    h("label", { class: "field" }, h("span", {}, "회선 속도 (Mbps)"), port,
      h("div", { class: "hint" }, "서버 업체가 보장하는 속도 (예: OVH VPS-2 = 1000, VPS-1 = 500). 용량 계산에서 아래 '회선 사용' 안전선까지 씁니다.")),
    h("label", { class: "field" }, h("span", {}, "월 트래픽 한도 (TB)"), traffic,
      h("div", { class: "hint" }, "0 = 무제한 (OVH 북미·유럽). 한도가 있는 업체면 숫자를 넣으세요 (예: Contabo 32) — 한도의 87.5%를 목표로 계산합니다.")),
    h("div", { class: "field" }, h("span", {}, "안전선"),
      h("div", { class: "safe-grid" }, memF, cpuF, portF, diskF),
      h("div", { class: "hint" }, "'몇 개 더 추가 가능'은 메모리·CPU·회선이 이 선을 넘지 않는 만큼으로 계산합니다. 라이브 화면 서버 박스 막대의 세로 눈금이 이 선이에요. 디스크는 계산에 안 넣고 넘으면 색만 바뀝니다.")),
    h("div", { class: "field" }, h("span", {}, "음악 프로그램"), sharedLab,
      h("div", { class: "hint" + (pendingShared ? " warn" : "") }, pendingShared
        ? `저장됨 — 다음 업데이트(또는 SSH 에서 sudo livemax restart) 때 적용됩니다. 지금은 ${s.settings.audio_shared_active ? "묶음" : "라이브마다 하나"}로 돌고 있어요.`
        : `지금: ${s.settings.audio_shared_active ? "채널별 묶음" : "라이브마다 하나"}. 바꾸면 다음 업데이트(또는 sudo livemax restart) 때 적용됩니다 — 그때 라이브들이 차례로 다시 켜져요.`),
      h("div", { class: "row", style: "margin-top:10px;gap:8px;align-items:center" }, h("span", {}, "묶음 하나에 최대"), groupMax, h("span", {}, "개 (기본 10)")),
      h("div", { class: "hint" }, "한 채널에 라이브가 이보다 많으면 나눠 묶습니다 (예: 25개 → 10·10·5). 이미 자리를 잡은 라이브는 옮기지 않고(음악이 끊기지 않게), 새로 만드는 라이브와 다음 업데이트부터 적용됩니다.")),
    h("button", { class: "btn primary", onclick: async () => {
      try {
        await api("PUT", "/api/system/settings", { server_name: name.value, max_lives: Number(maxLives.value),
          port_mbit: Number(port.value), monthly_traffic_tb: Number(traffic.value),
          mem_reserve_mb: Number(memR.value), cpu_target_pct: Number(cpuT.value), port_use_pct: Number(portU.value), disk_warn_pct: Number(diskW.value),
          audio_shared: shared.checked, audio_group_max: Number(groupMax.value) });
        setServerName(name.value); toast("저장했습니다"); showSystem();
      } catch (e) { toast(e.message, true); }
    } }, "저장"));
}

function notifyCard(n, card) {
  // 휴대폰 알림: 보낼 알림 종류는 공통, 받는 곳은 Pushbullet · 텔레그램 (둘 다 켜도 됨)
  const toggle = (checked, label) => {
    const cb = h("input", { type: "checkbox" }); cb.checked = checked;
    const lab = h("label", { class: "check" + (checked ? " on" : ""), style: "margin-bottom:10px" }, cb, h("span", { class: "nm" }, label));
    cb.addEventListener("change", () => lab.classList.toggle("on", cb.checked));
    return [cb, lab];
  };
  const [en, enLab] = toggle(n.enabled, "Pushbullet 으로 받기");
  const [tgEn, tgEnLab] = toggle(n.tg_enabled, "텔레그램으로 받기");
  const [tgCmd, tgCmdLab] = toggle(n.tg_commands, "이 서버에서 명령 받기 (/상태 /오늘 /라이브 번호)");
  const tok = h("input", { class: "input", type: "password", autocomplete: "off",
    placeholder: n.token_masked ? `현재 토큰 ${n.token_masked} — 바꿀 때만 입력` : "o.xxxxxxxxxxxxxxxx (Access Token)" });
  const tgTok = h("input", { class: "input", type: "password", autocomplete: "off",
    placeholder: n.tg_token_masked ? `현재 토큰 ${n.tg_token_masked} — 바꿀 때만 입력` : "123456789:ABC... (BotFather 가 준 토큰)" });
  const cats = h("div", { class: "checks one" }, n.catalog.map((c) => {
    const cb = h("input", { type: "checkbox", value: c.key }); cb.checked = !!n.categories[c.key];
    const lab = h("label", { class: "check" + (cb.checked ? " on" : "") }, cb, h("span", { class: "nm" }, h("b", {}, c.label), h("span", { class: "desc" }, c.desc)));
    cb.addEventListener("change", () => lab.classList.toggle("on", cb.checked));
    return lab;
  }));
  const msg = h("div", { class: "msg err" }, [n.last_error ? `Pushbullet 최근 실패: ${n.last_error}` : "", n.tg_last_error ? `  텔레그램 최근 실패: ${n.tg_last_error}` : ""].join(""));
  const save = async () => {
    msg.textContent = "";
    const categories = Object.fromEntries([...cats.querySelectorAll("input")].map((x) => [x.value, x.checked]));
    try {
      const r = await api("PUT", "/api/system/notify", { enabled: en.checked, token: tok.value, categories,
        tg_enabled: tgEn.checked, tg_token: tgTok.value, tg_commands: tgCmd.checked });
      toast(r.message); showSystem();
    } catch (e) { msg.textContent = e.message; }
  };
  const btn = (label, fn, dis, cls) => h("button", { class: "btn " + (cls || ""), disabled: !!dis, onclick: fn }, label);
  const call = async (method, url, body, after) => { try { const r = await api(method, url, body); toast(r.message); if (after) showSystem(); } catch (e) { toast(e.message, true); } };
  return card("휴대폰 알림",
    h("span", { class: "flabel" }, "보낼 알림 (Pushbullet · 텔레그램 공통)"), cats,
    h("p", { class: "hint" }, `알림 제목 앞에 [${n.label}] 이 붙습니다 (서버명은 왼쪽 '서버' 칸). 같은 내용은 10분에 한 번, 시간당 최대 20건까지만 보냅니다.`),

    h("div", { class: "notify-sec" },
      h("div", { class: "notify-h" }, "텔레그램 ", h("span", { class: "faint small" }, "— 빠르고 무료 (추천)")),
      tgEnLab,
      h("label", { class: "field" }, h("span", {}, "텔레그램 봇 토큰"), tgTok,
        h("div", { class: "hint" }, "① 텔레그램에서 @BotFather 검색 → /newbot → 이름·아이디(끝이 bot) 정하기 → 받은 토큰을 여기에 붙여 넣기 ",
          "② 만든 봇을 열고 '시작(Start)' 누르기 ③ 아래 '연결 확인' — 대화방을 자동으로 찾아 연결하고 테스트 메시지를 보냅니다.")),
      n.tg_chat ? h("p", { class: "hint", style: "margin:-4px 0 10px" }, `연결된 대화방: ${n.tg_chat}`) : null,
      tgCmdLab,
      h("p", { class: "hint", style: "margin:-4px 0 10px" }, "대화방에 /상태 · /오늘 · /라이브 18 을 보내면 답합니다 (읽기 전용). 서버가 여러 대이고 같은 봇을 쓰면 한 서버에서만 켜세요."),
      h("div", { class: "row" },
        btn("연결 확인", () => call("POST", "/api/system/notify/telegram/connect", { tg_token: tgTok.value }, true), false, "primary"),
        btn("테스트 알림", () => call("POST", "/api/system/notify/telegram/test"), !n.tg_chat),
        n.tg_token_masked ? btn("토큰 삭제", async () => {
          if (!confirm("텔레그램 봇 토큰을 지우고 텔레그램 알림을 끌까요?")) return;
          call("PUT", "/api/system/notify", { clear_tg_token: true }, true);
        }, false, "ghost") : null)),

    h("div", { class: "notify-sec" },
      h("div", { class: "notify-h" }, "Pushbullet"),
      enLab,
      h("label", { class: "field" }, h("span", {}, "Pushbullet Access Token"), tok,
        h("div", { class: "hint" }, "휴대폰에 Pushbullet 앱 설치·로그인 → PC에서 pushbullet.com → Settings → Access Tokens → Create Access Token 으로 만든 값을 붙여 넣으세요. 무료는 월 500건까지.")),
      h("div", { class: "row" },
        btn("테스트 알림", () => call("POST", "/api/system/notify/test"), !n.token_masked),
        n.token_masked ? btn("토큰 삭제", async () => {
          if (!confirm("저장된 토큰을 지우고 Pushbullet 알림을 끌까요?")) return;
          call("PUT", "/api/system/notify", { clear_token: true, enabled: false }, true);
        }, false, "ghost") : null)),
    msg,
    h("div", { class: "row", style: "margin-top:12px" }, btn("저장", save, false, "primary")));
}

// ------------------------------------------------------------------ 라우터
async function route() {
  closeAllModals();
  let sess;
  try { sess = await api("GET", "/api/session"); } catch (e) { return; }
  setServerName(sess.server_name);
  LIVES_VIEW = ["list", "cctv"].includes(sess.lives_view) ? sess.lives_view : "grid";
  if (!sess.authenticated) { showLogin(); return; }
  if (sess.must_change) { mount(shell("lives")); showChangePassword(true); return; }
  const r = location.hash.replace(/^#\/?/, "");
  if (r.startsWith("media/video")) return showMedia("video");
  if (r.startsWith("media/audio")) return showMedia("audio");
  if (r.startsWith("system")) return showSystem();
  if (r.startsWith("channels")) return showChannels();
  return showLives();
}
window.addEventListener("hashchange", route);
route();
