// huntaway dashboard. No framework, no build step for the script, no network calls beyond this machine.

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage may be blocked */ } },
  sget(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  sset(k, v) { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } },
};

// The token: injected into the page on localhost, passed in the URL by `huntaway ui`, or typed in.
let TOKEN = $('meta[name="huntaway-token"]').content;
if (!TOKEN || TOKEN.startsWith("__")) TOKEN = "";
const hashToken = new URLSearchParams(location.hash.split("?")[0].replace(/^#/, "")).get("token");
if (hashToken) { store.sset("huntaway-token", hashToken); TOKEN = hashToken; history.replaceState(null, "", "#/overview"); }
if (!TOKEN) TOKEN = store.sget("huntaway-token") || "";

const fmt = {
  bytes(n) {
    n = Number(n || 0);
    if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(n >= 10 * (1 << 30) ? 0 : 1)} GB`;
    if (n >= 1 << 20) return `${Math.round(n / (1 << 20))} MB`;
    if (n >= 1 << 10) return `${Math.round(n / (1 << 10))} KB`;
    return `${Math.round(n)} B`;
  },
  rate(n) { return `${fmt.bytes(n)}/s`; },
  pct(n, d = 0) { return n == null ? "n/a" : `${Number(n).toFixed(d)}%`; },
  dur(s) {
    s = Math.max(0, Math.round(Number(s || 0)));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) { const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return m ? `${h}h ${m}m` : `${h}h`; }
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600); return h ? `${d}d ${h}h` : `${d}d`;
  },
  ago(ts) { return ts ? `${fmt.dur(Date.now() / 1000 - ts)} ago` : "never"; },
  time(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); },
  clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); },
  date(ts) { return new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); },
  day(ts) { return new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" }); },
  path(p) { return String(p || "").replace(/^\/(home|Users)\/[^/]+/, "~"); },
};

class ApiError extends Error {}

async function api(name, params = {}, post = false) {
  const headers = { "Authorization": `Bearer ${TOKEN}` };
  let res;
  if (post) {
    res = await fetch(`/api/${name}`, { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(params) });
  } else {
    const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]));
    res = await fetch(`/api/${name}?${q}`, { headers });
  }
  if (res.status === 401) { showLogin(); throw new ApiError("token required"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || res.statusText);
  return data;
}

function toast(text, kind = "ok") {
  const el = document.createElement("div");
  const tone = kind === "bad" ? "border-red-200 dark:border-red-500/30" : "border-ink-100 dark:border-ink-700";
  el.className = `card ${tone} px-4 py-3 text-sm shadow-lg`;
  el.innerHTML = `<div class="flex gap-3"><span class="mt-1.5 h-2 w-2 shrink-0 rounded-full ${kind === "bad" ? "bg-red-500" : "bg-tan-500"}"></span><span>${esc(text)}</span></div>`;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "bad" ? 9000 : 5000);
}

function openModal(html, wide = false) {
  const m = $("#modal");
  m.innerHTML = `<div class="card flex max-h-[92vh] w-full ${wide ? "sm:max-w-4xl" : "sm:max-w-lg"} flex-col overflow-hidden rounded-b-none sm:rounded-2xl">${html}</div>`;
  m.classList.remove("hidden");
  m.classList.add("flex");
  m.onclick = (e) => { if (e.target === m) closeModal(); };
  return m.firstElementChild;
}
function closeModal() { const m = $("#modal"); m.classList.add("hidden"); m.classList.remove("flex"); m.innerHTML = ""; }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function confirmBox(title, body, okText = "Confirm", danger = false) {
  return new Promise((resolve) => {
    const box = openModal(`<div class="card-pad"><h2 class="card-title">${esc(title)}</h2><div class="mt-2 text-sm text-ink-500 dark:text-ink-300">${body}</div>
      <div class="mt-6 flex justify-end gap-2"><button class="btn-ghost" data-no>Cancel</button><button class="${danger ? "btn-danger" : "btn-primary"}" data-yes>${esc(okText)}</button></div></div>`);
    $("[data-no]", box).onclick = () => { closeModal(); resolve(false); };
    $("[data-yes]", box).onclick = () => { closeModal(); resolve(true); };
  });
}

function showLogin() {
  const box = openModal(`<form class="card-pad"><div class="flex items-center gap-3"><img src="/static/logo.svg" class="h-9 w-9" alt=""><h2 class="card-title">Token needed</h2></div>
    <p class="mt-3 text-sm text-ink-500 dark:text-ink-300">This dashboard is open to your network, so it asks for the token. On the machine running huntaway, run <code class="font-mono">huntaway token</code>.</p>
    <input class="input mt-4 font-mono" name="t" autocomplete="off" placeholder="paste the token"><div class="mt-4 flex justify-end"><button class="btn-primary">Open</button></div></form>`);
  $("form", box).onsubmit = (e) => { e.preventDefault(); TOKEN = e.target.t.value.trim(); store.sset("huntaway-token", TOKEN); closeModal(); route(); connectLive(); };
}

// Icons (stroke, 24px grid)
const ICON = {
  overview: '<path d="M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 3v6h8V3z"/>',
  agents: '<path d="M12 8V4H8"/><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
  sessions: '<path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  analytics: '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>',
  security: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  events: '<path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="9"/>',
  rules: '<path d="M4 6h10M4 12h16M4 18h7"/><circle cx="17" cy="6" r="2"/><circle cx="14" cy="18" r="2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  integrations: '<path d="M9 7H6a3 3 0 0 0 0 6h3M15 7h3a3 3 0 0 1 0 6h-3M8 10h8"/><path d="M12 17v4"/>',
};
const icon = (name, cls = "h-[18px] w-[18px]") => `<svg viewBox="0 0 24 24" class="${cls}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] || ""}</svg>`;

const NAV = [
  ["Watch", [["overview", "Overview"], ["agents", "Agents"], ["analytics", "Analytics"]]],
  ["Keep", [["sessions", "Saved sessions"], ["security", "Security"], ["events", "Events"]]],
  ["Configure", [["rules", "Rules and protection"], ["settings", "Settings"], ["integrations", "Integrations"]]],
];

function renderNav(active) {
  $("#nav").innerHTML = NAV.map(([group, items]) => `<div><div class="label mb-2 px-3">${group}</div><div class="flex flex-col gap-1">${
    items.map(([id, label]) => `<a href="#/${id}" class="nav-link ${id === active ? "active" : ""}">${icon(id)}<span>${label}</span><span class="ml-auto" data-badge="${id}"></span></a>`).join("")}</div></div>`).join("");
}

// Charts: small, dependency free SVG.
const COLORS = { cpu: "#e0953f", mem: "#6d7cff", swap: "#a78bfa", io: "#38bdf8", net: "#34d399", temp: "#f87171", agents: "#e0953f", psi: "#f59e0b" };
let gradId = 0;

function niceMax(v) {
  if (!isFinite(v) || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function sparkline(values, color = COLORS.cpu, w = 120, h = 36, max = null) {
  const vals = values.filter((v) => v != null);
  if (vals.length < 2) return `<svg width="${w}" height="${h}"></svg>`;
  const top = max ?? Math.max(...vals, 1);
  const step = w / (values.length - 1);
  let d = "";
  values.forEach((v, i) => { if (v == null) return; const x = i * step, y = h - 2 - (v / top) * (h - 4); d += `${d ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`; });
  const id = `g${++gradId}`;
  return `<svg viewBox="0 0 ${w} ${h}" class="h-9 w-full" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d}L${w},${h}L0,${h}Z" fill="url(#${id})"/><path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}

function chart(el, { ts, series, max = null, unit = (v) => v, height = 220, timeFmt = null }) {
  el.classList.add("relative");
  const draw = () => {
    const w = Math.max(280, el.clientWidth);
    const pad = { l: 44, r: 10, t: 10, b: 26 };
    const iw = w - pad.l - pad.r, ih = height - pad.t - pad.b;
    const all = series.flatMap((s) => s.values.filter((v) => v != null));
    const top = max ?? niceMax(Math.max(...all, 0) * 1.1);
    const n = ts.length;
    if (n < 2) { el.innerHTML = `<div class="flex h-[${height}px] items-center justify-center text-sm text-ink-300">Collecting data. Charts fill in as huntaway samples.</div>`; return; }
    const x = (i) => pad.l + (i / (n - 1)) * iw;
    const y = (v) => pad.t + ih - (v / top) * ih;
    let grid = "";
    for (let k = 0; k <= 4; k++) {
      const v = (top / 4) * k, yy = y(v);
      grid += `<line x1="${pad.l}" x2="${w - pad.r}" y1="${yy}" y2="${yy}" class="stroke-ink-100 dark:stroke-ink-800" stroke-dasharray="${k ? "3 4" : ""}"/>`;
      grid += `<text x="${pad.l - 8}" y="${yy + 4}" text-anchor="end" class="fill-ink-300 text-[10px] num">${esc(unit(v))}</text>`;
    }
    const span = ts[n - 1] - ts[0];
    const tf = timeFmt || (span > 3 * 86400 ? fmt.day : span < 900 ? fmt.clock : fmt.time);
    const ticks = Math.min(6, Math.floor(iw / 90));
    for (let k = 0; k <= ticks; k++) {
      const i = Math.round((k / ticks) * (n - 1));
      grid += `<text x="${x(i)}" y="${height - 6}" text-anchor="${k === 0 ? "start" : k === ticks ? "end" : "middle"}" class="fill-ink-300 text-[10px]">${esc(tf(ts[i]))}</text>`;
    }
    let paths = "", defs = "";
    for (const s of series) {
      let d = "", seg = "";
      const parts = [];
      s.values.forEach((v, i) => {
        if (v == null) { if (seg) parts.push(seg); seg = ""; return; }
        seg += `${seg ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      });
      if (seg) parts.push(seg);
      d = parts.join("");
      if (s.area !== false) {
        const id = `c${++gradId}`;
        defs += `<linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity=".22"/><stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`;
        for (const p of parts) {
          const first = p.slice(1).split("L")[0].split(",")[0], last = p.split("L").pop().split(",")[0].replace("M", "");
          paths += `<path d="${p}L${last},${pad.t + ih}L${first},${pad.t + ih}Z" fill="url(#${id})"/>`;
        }
      }
      paths += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
    el.innerHTML = `<svg viewBox="0 0 ${w} ${height}" width="100%" height="${height}" role="img" aria-label="${esc(series.map((s) => s.name).join(", "))} chart"><defs>${defs}</defs>${grid}${paths}
      <line class="hair stroke-ink-300 dark:stroke-ink-600" x1="0" x2="0" y1="${pad.t}" y2="${pad.t + ih}" visibility="hidden"/>
      <rect x="${pad.l}" y="${pad.t}" width="${iw}" height="${ih}" fill="transparent" class="hit"/></svg><div class="tip hidden"></div>`;
    const svg = $("svg", el), tip = $(".tip", el), hair = $(".hair", el);
    $(".hit", el).addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * w;
      const i = Math.max(0, Math.min(n - 1, Math.round(((px - pad.l) / iw) * (n - 1))));
      hair.setAttribute("x1", x(i)); hair.setAttribute("x2", x(i)); hair.setAttribute("visibility", "visible");
      tip.innerHTML = `<div class="mb-1 font-medium text-ink-900 dark:text-white">${esc(fmt.date(ts[i]))}</div>` + series.map((s) => `<div class="flex items-center gap-2"><span class="h-2 w-2 rounded-full" style="background:${s.color}"></span><span class="text-ink-400">${esc(s.name)}</span><span class="ml-auto num font-medium">${s.values[i] == null ? "n/a" : esc(unit(s.values[i]))}</span></div>`).join("");
      tip.classList.remove("hidden");
      const left = (x(i) / w) * r.width;
      tip.style.left = `${Math.min(r.width - 170, Math.max(0, left + 12))}px`;
      tip.style.top = "8px";
    });
    $(".hit", el).addEventListener("mouseleave", () => { tip.classList.add("hidden"); hair.setAttribute("visibility", "hidden"); });
  };
  draw();
  const ro = new ResizeObserver(() => draw());
  ro.observe(el);
  el._ro = ro;
}

function donut(segments, size = 132, stroke = 16) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;
  const r = (size - stroke) / 2, c = 2 * Math.PI * r;
  let off = 0;
  const arcs = segments.map((s) => {
    const len = (s.value / total) * c;
    const a = `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${s.color}" stroke-width="${stroke}" stroke-dasharray="${len} ${c - len}" stroke-dashoffset="${-off}" transform="rotate(-90 ${size / 2} ${size / 2})"/>`;
    off += len;
    return a;
  }).join("");
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" class="stroke-ink-50 dark:stroke-ink-800" stroke-width="${stroke}"/>${arcs}</svg>`;
}

function meter(percent, tone = null) {
  const p = Math.max(0, Math.min(100, percent || 0));
  const color = tone || (p >= 90 ? "bg-red-500" : p >= 75 ? "bg-amber-500" : "bg-tan-500");
  return `<div class="h-1.5 w-full overflow-hidden rounded-full bg-ink-50 dark:bg-ink-800"><div class="h-full rounded-full ${color}" style="width:${p}%"></div></div>`;
}

const HARNESS = {
  claude: ["Claude Code", "#d97757"], codex: ["Codex", "#10a37f"], opencode: ["OpenCode", "#6d7cff"], gemini: ["Gemini CLI", "#4f8df5"],
  qwen: ["Qwen Code", "#7c5cff"], aider: ["Aider", "#14b8a6"], cursor: ["Cursor Agent", "#94a3b8"], goose: ["Goose", "#f59e0b"],
  amp: ["Amp", "#ef4444"], copilot: ["Copilot CLI", "#8b5cf6"], crush: ["Crush", "#ec4899"], kiro: ["Kiro", "#a855f7"],
};
const harnessChip = (h, label) => {
  const [name, color] = HARNESS[h] || [label || h, "#94a3b8"];
  return `<span class="chip-mute whitespace-nowrap"><span class="h-2 w-2 rounded-full" style="background:${color}"></span>${esc(label || name)}</span>`;
};
const SEV = { critical: "chip-bad", high: "chip-bad", medium: "chip-warn", low: "chip-tan", info: "chip-mute" };
const KIND_TONE = { close: "bg-tan-500", terminate: "bg-red-500", kill: "bg-red-600", throttle: "bg-amber-500", alert: "bg-amber-400", archive: "bg-sky-500", judge: "bg-violet-500", config: "bg-ink-400", service: "bg-ink-300", finding: "bg-red-400" };

function statusChip(a) {
  if (a.status === "busy") return `<span class="chip-ok">working</span>`;
  if (a.status === "waiting") return `<span class="chip-warn">waiting for you</span>`;
  if (a.status === "idle") return `<span class="chip-mute">idle</span>`;
  return `<span class="chip-mute">${a.interactive ? "open" : "headless"}</span>`;
}

function eventRow(e) {
  return `<li class="flex gap-3 py-2.5"><span class="mt-1.5 h-2 w-2 shrink-0 rounded-full ${KIND_TONE[e.kind] || "bg-ink-300"}"></span>
    <div class="min-w-0 flex-1"><p class="text-sm leading-snug text-ink-700 dark:text-ink-100">${esc(e.title)}</p>
    <p class="mt-0.5 text-xs text-ink-400 dark:text-ink-300">${esc(e.kind)} · ${esc(fmt.date(e.ts))}</p></div></li>`;
}

function empty(text, sub = "") {
  return `<div class="flex flex-col items-center justify-center px-6 py-14 text-center"><img src="/static/logo.svg" class="h-12 w-12 opacity-80" alt=""><p class="mt-4 font-medium text-ink-700 dark:text-ink-100">${esc(text)}</p>${sub ? `<p class="mt-1 max-w-md text-sm text-ink-400 dark:text-ink-300">${sub}</p>` : ""}</div>`;
}

function markdown(text) {
  const lines = String(text).split("\n");
  let html = "", inCode = false, code = [], para = [];
  const flush = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\[([^\]]{1,80})\]/g, '<mark class="rounded bg-tan-100 px-0.5 dark:bg-tan-500/30 dark:text-white">$1</mark>');
  for (const line of lines) {
    if (line.startsWith("```")) { if (inCode) { html += `<pre>${esc(code.join("\n"))}</pre>`; code = []; inCode = false; } else { flush(); inCode = true; } continue; }
    if (inCode) { code.push(line); continue; }
    if (line.startsWith("# ")) { flush(); html += `<h1>${inline(line.slice(2))}</h1>`; }
    else if (line.startsWith("### ")) { flush(); html += `<h3>${inline(line.slice(4))}</h3>`; }
    else if (line.startsWith("> ")) { flush(); html += `<blockquote>${inline(line.slice(2))}</blockquote>`; }
    else if (!line.trim()) flush();
    else para.push(line);
  }
  if (inCode) html += `<pre>${esc(code.join("\n"))}</pre>`;
  flush();
  return html;
}

async function copy(text, what = "Copied") {
  try { await navigator.clipboard.writeText(text); toast(what); } catch { toast("Copy failed: select and copy by hand", "bad"); }
}

// Live data
const live = { samples: [], last: null, listeners: new Set() };
let source = null;

// ?still serves the page without the live stream, for screenshots and printing.
const STILL = new URLSearchParams(location.search).has("still");

function connectLive() {
  if (source) source.close();
  if (!TOKEN && !location.hostname.match(/^(localhost|127\.)/)) return;
  api("live_samples").then((rows) => { live.samples = rows; if (rows.length) onSample(rows[rows.length - 1], false); }).catch(() => {});
  if (STILL) return;
  source = new EventSource(`/api/stream?token=${encodeURIComponent(TOKEN)}`);
  source.onmessage = (ev) => { const s = JSON.parse(ev.data); live.samples.push(s); if (live.samples.length > 720) live.samples.shift(); onSample(s, true); };
  source.onerror = () => { $("#live").className = "h-2 w-2 rounded-full bg-amber-500"; $("#live-text").textContent = "reconnecting"; };
}

function onSample(s, fresh) {
  live.last = s;
  $("#live").className = "live-dot h-2 w-2 rounded-full bg-emerald-500";
  $("#live-text").textContent = `live · ${s.cost_ms} ms`;
  const lv = $("#level");
  lv.className = s.level === "critical" ? "chip-bad hidden sm:inline-flex" : s.level === "tight" ? "chip-warn hidden sm:inline-flex" : "chip-ok hidden sm:inline-flex";
  lv.textContent = s.level === "ok" ? "memory ok" : `memory ${s.level}`;
  const badge = $('[data-badge="agents"]');
  if (badge) badge.innerHTML = s.agents_stale ? `<span class="chip-warn">${s.agents_stale}</span>` : "";
  for (const fn of live.listeners) fn(s, fresh);
}

async function loadService() {
  try {
    const st = await api("status");
    $("#svc-dot").className = `h-2 w-2 rounded-full ${st.running ? "bg-emerald-500" : "bg-amber-500"}`;
    $("#svc-text").textContent = st.running ? `Guarding · ${st.mode}` : "Not running";
    $("#svc-foot").textContent = `v${st.version} · ${fmt.bytes(st.service.rss)} RAM`;
    const m = $("#mode");
    m.className = st.mode === "observe" ? "chip-info hidden sm:inline-flex" : "chip-tan hidden sm:inline-flex";
    m.textContent = st.mode === "observe" ? "observe only" : "enforcing";
    return st;
  } catch { return null; }
}

// Pages
const pages = {};

pages.overview = {
  title: "Overview", sub: "The machine right now, and what huntaway did about it",
  async render(el) {
    const [st, procs, agents] = await Promise.all([api("status"), api("processes", { sort: "mem", limit: 8 }), api("agents_list")]);
    const s = st.system;
    const kpi = (id, label, value, sub, color, pct) => `<div class="card card-pad"><div class="flex items-center justify-between"><span class="label">${label}</span><span class="text-xs text-ink-300 num" data-k="${id}-sub">${sub}</span></div>
      <div class="mt-2 flex items-end justify-between gap-3"><span class="text-3xl font-semibold tracking-tight text-ink-900 num dark:text-white" data-k="${id}">${value}</span><div class="w-28" data-spark="${id}"></div></div>
      <div class="mt-3" data-meter="${id}">${meter(pct)}</div></div>`;
    const interactive = agents.filter((a) => a.interactive);
    const stale = interactive.filter((a) => a.stale);
    const byH = {};
    for (const a of agents) byH[a.harness] = (byH[a.harness] || 0) + a.rss;
    const segs = Object.entries(byH).sort((a, b) => b[1] - a[1]).map(([h, v]) => ({ value: v, color: (HARNESS[h] || [h, "#94a3b8"])[1], h }));
    const sevs = st.findings;
    const serious = (sevs.critical || 0) + (sevs.high || 0);
    el.innerHTML = `
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        ${kpi("cpu", "CPU", fmt.pct(s.cpu), `load ${s.load[0]} · ${s.cores} cores`, COLORS.cpu, s.cpu)}
        ${kpi("mem", "Memory", fmt.pct(s.mem), `${fmt.bytes(s.mem_available)} free`, COLORS.mem, s.mem)}
        ${kpi("swap", "Swap", s.swap_total ? fmt.pct(s.swap) : "none", s.swap_total ? `${fmt.bytes(s.swap_used)} used` : "no swap", COLORS.swap, s.swap)}
        ${kpi("psi", "Stalls", s.psi && s.psi.memory != null ? fmt.pct(s.psi.memory, 1) : "n/a", "memory, last 10 s", COLORS.psi, (s.psi && s.psi.memory) || 0)}
      </div>
      <div class="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div class="card card-pad xl:col-span-2"><div class="flex flex-wrap items-center justify-between gap-2"><div><h2 class="card-title">Live</h2><p class="card-sub">CPU and memory, the last ten minutes</p></div>
          <div class="flex gap-3 text-xs text-ink-400"><span class="flex items-center gap-1.5"><span class="h-2 w-2 rounded-full" style="background:${COLORS.cpu}"></span>CPU</span><span class="flex items-center gap-1.5"><span class="h-2 w-2 rounded-full" style="background:${COLORS.mem}"></span>Memory</span><span class="flex items-center gap-1.5"><span class="h-2 w-2 rounded-full" style="background:${COLORS.swap}"></span>Swap</span></div></div>
          <div class="mt-4" id="live-chart"></div></div>
        <div class="card card-pad flex flex-col"><div class="flex items-center justify-between"><div><h2 class="card-title">Agent sessions</h2><p class="card-sub">${interactive.length} open in terminals</p></div><a href="#/agents" class="btn-ghost btn-sm">Open</a></div>
          <div class="mt-4 flex items-center gap-5">${donut(segs.length ? segs : [{ value: 1, color: "#e5e7eb" }])}
            <div class="min-w-0 flex-1 space-y-1.5">${segs.slice(0, 5).map((g) => `<div class="flex items-center gap-2 text-sm"><span class="h-2 w-2 rounded-full" style="background:${g.color}"></span><span class="truncate">${esc((HARNESS[g.h] || [g.h])[0])}</span><span class="ml-auto num text-ink-400">${fmt.bytes(g.value)}</span></div>`).join("") || '<p class="text-sm text-ink-400">No agents running.</p>'}</div></div>
          <div class="mt-auto pt-5"><div class="rounded-xl ${stale.length ? "bg-tan-50 dark:bg-tan-500/10" : "bg-ink-50 dark:bg-ink-850"} p-4">
            <p class="text-sm"><span class="font-semibold text-ink-900 num dark:text-white">${stale.length} stale</span> <span class="text-ink-500 dark:text-ink-300">holding ${fmt.bytes(stale.reduce((a, x) => a + x.rss, 0))}</span></p>
            <p class="mt-1 text-xs text-ink-400 dark:text-ink-300">Each is saved and checked before it closes, and stays resumable.</p>
            ${stale.length ? '<button class="btn-tan btn-sm mt-3" data-reap>Save and close them</button>' : ""}</div></div></div>
      </div>
      <div class="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div class="card xl:col-span-2"><div class="flex items-center justify-between px-5 pt-5 sm:px-6"><div><h2 class="card-title">Biggest processes</h2><p class="card-sub">By memory, with what protects each one</p></div><a href="#/rules" class="btn-ghost btn-sm">Protection</a></div>
          <div class="mt-3 overflow-x-auto"><table class="tbl"><thead><tr><th>Process</th><th class="text-right">CPU</th><th>Memory</th><th>Guard</th></tr></thead><tbody>${procs.map((p) => `<tr><td><div class="font-medium text-ink-800 dark:text-ink-100">${esc(p.name)}</div><div class="text-xs text-ink-400 num">pid ${p.pid}${p.agent ? ` · ${esc(p.agent)}` : ""}</div></td>
            <td class="text-right num">${fmt.pct(p.cpu, 1)}</td><td class="w-48"><div class="num text-xs text-ink-500 dark:text-ink-300">${fmt.bytes(p.rss)}</div><div class="mt-1">${meter((p.rss / s.mem_total) * 100 * 4)}</div></td>
            <td>${p.protected ? `<span class="chip-ok">${esc(p.protected)}</span>` : '<span class="chip-mute">watched</span>'}</td></tr>`).join("")}</tbody></table></div></div>
        <div class="card card-pad"><div class="flex items-center justify-between"><div><h2 class="card-title">Recent actions</h2><p class="card-sub">Every one is explained</p></div><a href="#/events" class="btn-ghost btn-sm">All</a></div>
          <ul class="mt-2 divide-y divide-ink-50 dark:divide-ink-800">${st.events.length ? st.events.slice(0, 7).map(eventRow).join("") : `<li class="py-6 text-sm text-ink-400">Nothing yet. huntaway acts only when a rule says so.</li>`}</ul></div>
      </div>
      <div class="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
        <a href="#/security" class="card card-pad block transition hover:border-tan-300"><span class="label">Security</span><div class="mt-2 flex items-center gap-3"><span class="text-2xl font-semibold num ${serious ? "text-red-600 dark:text-red-400" : "text-ink-900 dark:text-white"}">${serious}</span><span class="text-sm text-ink-500 dark:text-ink-300">serious findings · ${(sevs.medium || 0)} medium</span></div></a>
        <div class="card card-pad"><span class="label">huntaway itself</span><div class="mt-2 flex items-baseline gap-2"><span class="text-2xl font-semibold num text-ink-900 dark:text-white">${fmt.bytes(st.service.rss)}</span><span class="text-sm text-ink-500 dark:text-ink-300">RAM · ${st.service.cpu_percent}% CPU · ${st.service.sample_ms} ms per look</span></div></div>
        <div class="card card-pad"><span class="label">Saved for you</span><div class="mt-2 flex items-baseline gap-2"><span class="text-2xl font-semibold num text-ink-900 dark:text-white">${st.stats.closed}</span><span class="text-sm text-ink-500 dark:text-ink-300">sessions closed since start · ${fmt.bytes(st.stats.freed)} freed</span></div></div>
      </div>`;
    const reap = $("[data-reap]", el);
    if (reap) reap.onclick = () => reapFlow(stale);
    const drawLive = () => {
      const rows = live.samples.slice(-120);
      chart($("#live-chart", el), { ts: rows.map((r) => r.ts), max: 100, height: 268, unit: (v) => `${Math.round(v)}%`, series: [
        { name: "CPU", color: COLORS.cpu, values: rows.map((r) => r.cpu) },
        { name: "Memory", color: COLORS.mem, values: rows.map((r) => r.mem), area: false },
        { name: "Swap", color: COLORS.swap, values: rows.map((r) => (r.swap_total ? r.swap : null)), area: false },
      ] });
      for (const [k, f, c] of [["cpu", (r) => r.cpu, COLORS.cpu], ["mem", (r) => r.mem, COLORS.mem], ["swap", (r) => r.swap, COLORS.swap], ["psi", (r) => (r.psi || {}).memory ?? 0, COLORS.psi]]) {
        const box = $(`[data-spark="${k}"]`, el); if (box) box.innerHTML = sparkline(rows.slice(-60).map(f), c, 120, 36, k === "psi" ? null : 100);
      }
    };
    drawLive();
    return (sample) => {
      drawLive();
      const set = (k, v) => { const n = $(`[data-k="${k}"]`, el); if (n) n.textContent = v; };
      set("cpu", fmt.pct(sample.cpu)); set("mem", fmt.pct(sample.mem));
      set("swap", sample.swap_total ? fmt.pct(sample.swap) : "none");
      set("psi", sample.psi && sample.psi.memory != null ? fmt.pct(sample.psi.memory, 1) : "n/a");
      set("cpu-sub", `load ${sample.load[0]} · ${sample.cores} cores`); set("mem-sub", `${fmt.bytes(sample.mem_available)} free`);
      for (const [k, v] of [["cpu", sample.cpu], ["mem", sample.mem], ["swap", sample.swap], ["psi", (sample.psi || {}).memory || 0]]) { const m = $(`[data-meter="${k}"]`, el); if (m) m.innerHTML = meter(v); }
    };
  },
};

async function reapFlow(stale) {
  const list = stale.slice(0, 12).map((a) => `<li class="flex justify-between gap-3 py-1"><span class="truncate">${esc(a.title || fmt.path(a.cwd))}</span><span class="shrink-0 num text-ink-400">${fmt.dur(a.idle)} · ${fmt.bytes(a.rss)}</span></li>`).join("");
  const ok = await confirmBox(`Save and close ${stale.length} stale session${stale.length === 1 ? "" : "s"}?`, `<ul class="mt-2 text-sm">${list}</ul><p class="mt-3">Each transcript is saved and checked byte for byte first. Its terminal then shows the command that resumes it.</p>`, "Save and close");
  if (!ok) return;
  toast(`Saving and closing ${stale.length} sessions`);
  try {
    const r = await api("agents_reap", { dry_run: false }, true);
    const done = r.results.filter((x) => x.ok).length;
    toast(`Closed ${done} of ${r.results.length}. Freed about ${fmt.bytes(r.rss)}.`);
  } catch (e) { toast(e.message, "bad"); }
  route();
}

pages.agents = {
  title: "Agent sessions", sub: "Claude Code, Codex, OpenCode and the rest, with what each one holds",
  filter: store.get("huntaway-agents-filter", "open"),
  async render(el) {
    const rows = await api("agents_list");
    const f = this.filter;
    const shown = rows.filter((a) => (f === "all" ? true : f === "stale" ? a.stale : f === "busy" ? a.status === "busy" : a.interactive));
    const open = rows.filter((a) => a.interactive);
    const stale = open.filter((a) => a.stale);
    const stat = (label, value, sub) => `<div class="card card-pad"><span class="label">${label}</span><div class="mt-2 text-2xl font-semibold num text-ink-900 dark:text-white">${value}</div><div class="mt-1 text-xs text-ink-400 dark:text-ink-300">${sub}</div></div>`;
    el.innerHTML = `<div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
        ${stat("Open sessions", open.length, `${rows.length - open.length} more without a terminal`)}
        ${stat("Memory held", fmt.bytes(open.reduce((a, x) => a + x.rss, 0)), "by sessions in terminals")}
        ${stat("Stale", stale.length, `${fmt.bytes(stale.reduce((a, x) => a + x.rss, 0))} to free`)}
        ${stat("Working now", rows.filter((a) => a.status === "busy").length, "never closed while busy")}</div>
      <div class="card mt-4"><div class="flex flex-wrap items-center justify-between gap-3 px-5 pt-5 sm:px-6"><div class="seg" role="tablist">${["open", "stale", "busy", "all"].map((k) => `<button data-f="${k}" class="${k === f ? "on" : ""}">${k[0].toUpperCase() + k.slice(1)}</button>`).join("")}</div>
        <div class="flex gap-2">${stale.length ? `<button class="btn-tan btn-sm" data-reap>Save and close ${stale.length} stale</button>` : ""}<button class="btn-ghost btn-sm" data-refresh>Refresh</button></div></div>
        <div class="mt-3 overflow-x-auto">${shown.length ? `<table class="tbl"><thead><tr><th>Session</th><th>Harness</th><th>Status</th><th class="text-right">Idle</th><th>Memory</th><th>Verdict</th><th class="text-right">Actions</th></tr></thead><tbody>
        ${shown.map((a) => `<tr data-id="${esc(a.id)}"><td class="max-w-[22rem]"><div class="truncate font-medium text-ink-800 dark:text-ink-100" title="${esc(a.title)}">${esc(a.title || fmt.path(a.cwd) || "untitled")}</div><div class="truncate text-xs text-ink-400 num">${esc(fmt.path(a.cwd))} · pid ${a.pid}${a.tty ? ` · ${esc(a.tty.replace("/dev/", ""))}` : ""}</div></td>
          <td>${harnessChip(a.harness, a.label)}</td><td class="whitespace-nowrap">${statusChip(a)}${a.pinned ? ' <span class="chip-info">pinned</span>' : ""}${a.flags.length ? ' <span class="chip-warn" title="' + esc(a.flags.join(" ")) + '">checks off</span>' : ""}</td>
          <td class="whitespace-nowrap text-right num">${a.status === "busy" ? "now" : fmt.dur(a.idle)}</td><td class="whitespace-nowrap num">${fmt.bytes(a.rss)}</td>
          <td class="whitespace-nowrap">${a.stale ? '<span class="chip-tan">stale</span>' : `<span class="text-xs text-ink-400">${esc(a.why)}</span>`}</td>
          <td class="whitespace-nowrap text-right">${a.interactive ? `<button class="btn-ghost btn-sm" data-act="save">Save</button> <button class="btn-ghost btn-sm" data-act="${a.pinned ? "unpin" : "pin"}">${a.pinned ? "Unpin" : "Pin"}</button> <button class="btn-ghost btn-sm" data-act="close">Close</button>` : ""}</td></tr>`).join("")}</tbody></table>` : empty("No sessions here", "Agent sessions appear as soon as a harness runs.")}</div></div>
      <p class="mt-3 text-xs text-ink-400 dark:text-ink-300">A session is stale after ${fmt.dur((window.__cfg?.["agents.idle_hours"] ?? 12) * 3600)} with no message, keystroke or CPU work. Pinned and working sessions are never closed.</p>`;
    $$("[data-f]", el).forEach((b) => (b.onclick = () => { this.filter = b.dataset.f; store.set("huntaway-agents-filter", this.filter); this.render(el); }));
    $("[data-refresh]", el).onclick = () => this.render(el);
    const reap = $("[data-reap]", el); if (reap) reap.onclick = () => reapFlow(stale);
    $$("[data-act]", el).forEach((b) => (b.onclick = async () => {
      const id = b.closest("tr").dataset.id, act = b.dataset.act, a = rows.find((r) => r.id === id);
      try {
        if (act === "save") { const r = await api("agent_save", { id }, true); toast(`Saved ${fmt.bytes(r.size)} to the archive`); }
        if (act === "pin" || act === "unpin") { await api("agent_pin", { id, pinned: act === "pin" }, true); toast(act === "pin" ? "Pinned: never closed" : "Unpinned"); }
        if (act === "close") {
          if (!(await confirmBox("Save and close this session?", `<p><strong>${esc(a.title || a.cwd)}</strong></p><p class="mt-2">It is saved and checked first. Resume it later with:</p><pre class="mt-2 overflow-x-auto rounded-xl bg-ink-50 p-3 font-mono text-xs dark:bg-ink-950">${esc(a.resume || "the harness's resume command")}</pre>`, "Save and close"))) return;
          const r = await api("agent_close", { id }, true); toast(r.title || "Closed");
        }
      } catch (e) { toast(e.message, "bad"); }
      this.render(el);
    }));
  },
};

pages.sessions = {
  title: "Saved sessions", sub: "Every transcript huntaway kept, searchable by what was said",
  query: "", harness: "",
  async render(el) {
    const rows = await api("sessions", { query: this.query, harness: this.harness, limit: 100 });
    const harnesses = [...new Set(rows.map((r) => r.harness))];
    el.innerHTML = `<div class="card card-pad"><form class="flex flex-col gap-3 sm:flex-row" data-q><input class="input" name="q" value="${esc(this.query)}" placeholder="Find a session by anything said in it: a file, an error, a decision" autofocus>
        <select class="input sm:w-48" name="h"><option value="">Every harness</option>${Object.entries(HARNESS).map(([k, [n]]) => `<option value="${k}" ${k === this.harness ? "selected" : ""}>${n}</option>`).join("")}</select><button class="btn-primary">Search</button></form></div>
      <div class="mt-4 grid gap-3">${rows.length ? rows.map((r) => `<button class="card card-pad block w-full text-left transition hover:border-tan-300" data-id="${esc(r.id)}">
        <div class="flex flex-wrap items-center gap-2">${harnessChip(r.harness)}<span class="chip-mute">${esc(r.reason)}</span><span class="text-xs text-ink-400 num">${esc(fmt.date(r.archived))} · ${fmt.bytes(r.size)} · ${r.messages || 0} messages</span></div>
        <div class="mt-2 font-medium text-ink-900 dark:text-white">${esc(r.title || "untitled")}</div><div class="text-xs text-ink-400 num">${esc(fmt.path(r.cwd))}</div>
        ${r.snippet ? `<div class="md mt-2 text-sm text-ink-500 dark:text-ink-300">${markdown(r.snippet.replace(/\n/g, " "))}</div>` : ""}</button>`).join("") : empty(this.query ? "Nothing matches" : "No saved sessions yet", this.query ? "Try fewer or different words." : "Sessions are saved when huntaway closes them, when you press Save, and by the optional backup.")}</div>`;
    $("[data-q]", el).onsubmit = (e) => { e.preventDefault(); this.query = e.target.q.value; this.harness = e.target.h.value; this.render(el); };
    $$("[data-id]", el).forEach((b) => (b.onclick = () => openSession(b.dataset.id)));
    void harnesses;
  },
};

async function openSession(id) {
  const box = openModal(`<div class="flex items-center justify-between border-b border-ink-100 px-5 py-4 dark:border-ink-800"><h2 class="card-title">Loading</h2><button class="btn-ghost btn-sm" data-x>Close</button></div><div class="p-6 text-sm text-ink-400">Reading the transcript</div>`, true);
  $("[data-x]", box).onclick = closeModal;
  try {
    const d = await api("session_read", { id, length: 400000 });
    const s = d.session;
    box.innerHTML = `<div class="flex flex-wrap items-center gap-3 border-b border-ink-100 px-5 py-4 dark:border-ink-800"><div class="min-w-0 flex-1"><h2 class="card-title truncate">${esc(s.title || "Saved session")}</h2><p class="truncate text-xs text-ink-400 num">${esc(fmt.path(s.cwd))} · ${esc(fmt.date(s.archived))}</p></div>
      ${s.resume ? '<button class="btn-tan btn-sm" data-resume>Copy resume command</button>' : ""}<button class="btn-ghost btn-sm" data-restore>Restore files</button><button class="btn-ghost btn-sm" data-del>Delete</button><button class="btn-ghost btn-sm" data-x>Close</button></div>
      ${s.resume ? `<div class="border-b border-ink-100 bg-ink-50 px-5 py-2 font-mono text-xs text-ink-600 dark:border-ink-800 dark:bg-ink-950 dark:text-ink-300">${esc(s.resume)}</div>` : ""}
      <div class="md overflow-y-auto px-6 py-5 text-sm text-ink-700 dark:text-ink-200">${markdown(d.text)}${d.total > d.text.length ? `<p class="text-ink-400">The transcript continues: huntaway sessions show ${esc(id)}</p>` : ""}</div>`;
    $$("[data-x]", box).forEach((b) => (b.onclick = closeModal));
    const r = $("[data-resume]", box); if (r) r.onclick = () => copy(s.resume, "Resume command copied");
    $("[data-restore]", box).onclick = async () => { try { const x = await api("session_restore", { id }, true); toast(x.report.join("; ") || "Nothing to restore"); } catch (e) { toast(e.message, "bad"); } };
    $("[data-del]", box).onclick = async () => { if (!(await confirmBox("Delete this saved session?", "Its files are removed from the archive. The harness's own copy, if it still has one, is not touched.", "Delete", true))) return; try { await api("session_delete", { id }, true); toast("Deleted"); route(); } catch (e) { toast(e.message, "bad"); } };
  } catch (e) { toast(e.message, "bad"); closeModal(); }
}

pages.analytics = {
  title: "Analytics", sub: "What this machine did, from seconds to a year",
  range: store.get("huntaway-range", "24h"),
  async render(el) {
    const d = await api("analytics", { range: this.range });
    const s = d.series;
    const card = (id, title, sub, span = "") => `<div class="card card-pad ${span}"><h2 class="card-title">${title}</h2><p class="card-sub">${sub}</p><div class="mt-4" id="${id}"></div></div>`;
    const maxCpuH = Math.max(...d.apps.map((a) => a.cpu_hours), 0.001);
    const heat = d.heatmap;
    const heatMax = Math.max(...heat.flat().filter((v) => v != null), 1);
    const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const kinds = [...new Set(d.events.map((e) => e.kind))];
    const dayList = [...new Set(d.events.map((e) => e.day))].slice(-14);
    const perDay = dayList.map((day) => ({ day, parts: kinds.map((k) => ({ k, n: (d.events.find((e) => e.day === day && e.kind === k) || { n: 0 }).n })) }));
    const dayMax = Math.max(...perDay.map((p) => p.parts.reduce((a, x) => a + x.n, 0)), 1);
    el.innerHTML = `<div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div class="seg">${["1h", "6h", "24h", "7d", "30d", "90d", "1y"].map((r) => `<button data-r="${r}" class="${r === this.range ? "on" : ""}">${r}</button>`).join("")}</div>
        <span class="text-xs text-ink-400">${s.ts.length} points from ${esc(s.table.replace("samples", "raw samples").replace("raw samples_1m", "one minute averages").replace("raw samples_1h", "hourly averages"))}</span></div>
      <div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
        ${card("a-cpu", "CPU and load", "Share of all cores in use", "xl:col-span-2")}
        ${card("a-mem", "Memory and swap", "Share in use")}
        ${card("a-psi", "Pressure stalls", "Share of time tasks waited on CPU, memory or disk")}
        ${card("a-agents", "Memory held by agent sessions", "Resident memory of every agent process tree")}
        ${card("a-io", "Disk", "Read and write")}
        ${card("a-net", "Network", "Received and sent")}
        ${card("a-temp", "Temperature", "CPU package")}
        <div class="card card-pad"><h2 class="card-title">Top apps by CPU</h2><p class="card-sub">CPU hours and peak memory over the range</p><div class="mt-4 space-y-3">${d.apps.length ? d.apps.map((a) => `<div><div class="flex items-center justify-between text-sm"><span class="truncate">${esc(a.app)}</span><span class="num text-ink-400">${a.cpu_hours.toFixed(2)} h · ${fmt.bytes(a.rss_peak)}</span></div><div class="mt-1">${meter((a.cpu_hours / maxCpuH) * 100, "bg-tan-500")}</div></div>`).join("") : '<p class="text-sm text-ink-400">Collected per minute, so check back shortly.</p>'}</div></div>
        <div class="card card-pad"><h2 class="card-title">When the machine works</h2><p class="card-sub">Average CPU by weekday and hour, last four weeks</p><div class="mt-4 overflow-x-auto"><div class="grid min-w-[520px] gap-1" style="grid-template-columns:36px repeat(24,1fr)">
          ${days.map((dn, i) => `<span class="text-[10px] leading-5 text-ink-400">${dn}</span>${heat[i].map((v, h) => `<span class="h-5 rounded" title="${dn} ${h}:00 ${v == null ? "no data" : v + "%"}" style="background:${v == null ? "transparent" : `rgba(224,149,63,${0.12 + 0.88 * (v / heatMax)})`};${v == null ? "border:1px dashed rgba(148,163,184,.25)" : ""}"></span>`).join("")}`).join("")}
          <span></span>${Array.from({ length: 24 }, (_, h) => `<span class="text-center text-[9px] text-ink-300">${h % 6 === 0 ? h : ""}</span>`).join("")}</div></div></div>
        <div class="card card-pad"><h2 class="card-title">Actions per day</h2><p class="card-sub">What huntaway did, by kind</p><div class="mt-4 flex h-40 items-end gap-1.5">${perDay.length ? perDay.map((p) => `<div class="flex flex-1 flex-col-reverse overflow-hidden rounded-md" title="${esc(p.day)}: ${p.parts.map((x) => `${x.k} ${x.n}`).join(", ")}" style="height:${(p.parts.reduce((a, x) => a + x.n, 0) / dayMax) * 100}%">${p.parts.filter((x) => x.n).map((x) => `<div class="${KIND_TONE[x.k] || "bg-ink-300"}" style="flex:${x.n}"></div>`).join("")}</div>`).join("") : '<p class="text-sm text-ink-400">No actions yet.</p>'}</div>
          <div class="mt-3 flex flex-wrap gap-3 text-xs text-ink-400">${kinds.map((k) => `<span class="flex items-center gap-1.5"><span class="h-2 w-2 rounded-full ${KIND_TONE[k] || "bg-ink-300"}"></span>${esc(k)}</span>`).join("")}</div></div>
        <div class="card card-pad"><h2 class="card-title">Saved sessions</h2><p class="card-sub">By harness</p><div class="mt-4 flex items-center gap-6">${donut(d.sessions.map((x) => ({ value: x.n, color: (HARNESS[x.harness] || [x.harness, "#94a3b8"])[1] })).concat(d.sessions.length ? [] : [{ value: 1, color: "#e5e7eb" }]))}
          <div class="space-y-1.5 text-sm">${d.sessions.map((x) => `<div class="flex items-center gap-2"><span class="h-2 w-2 rounded-full" style="background:${(HARNESS[x.harness] || [x.harness, "#94a3b8"])[1]}"></span>${esc((HARNESS[x.harness] || [x.harness])[0])}<span class="ml-6 num text-ink-400">${x.n} · ${fmt.bytes(x.size)}</span></div>`).join("") || '<span class="text-ink-400">None yet</span>'}</div></div></div>
      </div>`;
    $$("[data-r]", el).forEach((b) => (b.onclick = () => { this.range = b.dataset.r; store.set("huntaway-range", this.range); this.render(el); }));
    const pctU = (v) => `${Math.round(v)}%`;
    chart($("#a-cpu", el), { ts: s.ts, max: 100, unit: pctU, series: [{ name: "CPU", color: COLORS.cpu, values: s.cpu }] });
    chart($("#a-mem", el), { ts: s.ts, max: 100, unit: pctU, series: [{ name: "Memory", color: COLORS.mem, values: s.mem }, { name: "Swap", color: COLORS.swap, values: s.swap, area: false }] });
    chart($("#a-psi", el), { ts: s.ts, unit: (v) => `${v.toFixed(v < 10 ? 1 : 0)}%`, series: [{ name: "Memory", color: COLORS.mem, values: s.psi_mem }, { name: "CPU", color: COLORS.cpu, values: s.psi_cpu, area: false }, { name: "Disk", color: COLORS.io, values: s.psi_io, area: false }] });
    chart($("#a-agents", el), { ts: s.ts, unit: (v) => fmt.bytes(v), series: [{ name: "Agents", color: COLORS.agents, values: s.agents_rss }] });
    chart($("#a-io", el), { ts: s.ts, unit: fmt.rate, series: [{ name: "Read", color: COLORS.io, values: s.disk_read }, { name: "Write", color: COLORS.swap, values: s.disk_write, area: false }] });
    chart($("#a-net", el), { ts: s.ts, unit: fmt.rate, series: [{ name: "Received", color: COLORS.net, values: s.net_rx }, { name: "Sent", color: COLORS.mem, values: s.net_tx, area: false }] });
    chart($("#a-temp", el), { ts: s.ts, unit: (v) => `${Math.round(v)} C`, series: [{ name: "CPU", color: COLORS.temp, values: s.temp }] });
  },
};

pages.security = {
  title: "Security", sub: "Quick checks every few hours, deep scans weekly",
  async render(el) {
    const d = await api("security");
    const counts = {};
    for (const f of d.findings) counts[f.severity] = (counts[f.severity] || 0) + 1;
    const serious = (counts.critical || 0) + (counts.high || 0);
    const quick = d.scans.filter((x) => x.source.startsWith("quick:"));
    const deep = d.scans.filter((x) => x.source.startsWith("deep:"));
    el.innerHTML = `<div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <div class="card card-pad lg:col-span-2"><div class="flex flex-wrap items-start justify-between gap-4"><div><span class="label">Posture</span><p class="mt-2 text-2xl font-semibold ${serious ? "text-red-600 dark:text-red-400" : "text-ink-900 dark:text-white"}">${serious ? `${serious} serious finding${serious === 1 ? "" : "s"}` : "Nothing serious found"}</p>
        <p class="mt-1 text-sm text-ink-500 dark:text-ink-300">${counts.medium || 0} medium · ${counts.low || 0} low · ${counts.info || 0} notes. Last deep scan ${d.last_deep ? fmt.ago(d.last_deep) : "never"}.</p></div>
        <div class="flex gap-2"><button class="btn-primary" data-scan>Run quick checks</button></div></div>
        <div class="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">${["critical", "high", "medium", "low"].map((k) => `<div class="rounded-xl bg-ink-50 p-3 dark:bg-ink-850"><span class="${SEV[k]}">${k}</span><div class="mt-2 text-xl font-semibold num">${counts[k] || 0}</div></div>`).join("")}</div></div>
      <div class="card card-pad"><span class="label">Hardening index</span><div class="mt-3 flex items-center gap-4">${d.hardening_index != null ? `${donut([{ value: d.hardening_index, color: "#e0953f" }, { value: 100 - d.hardening_index, color: "transparent" }], 96, 12)}<div><div class="text-3xl font-semibold num">${d.hardening_index}</div><div class="text-xs text-ink-400">Lynis, out of 100</div></div>` : '<p class="text-sm text-ink-400">Run a deep scan with Lynis installed to score the system.</p>'}</div></div></div>
      <div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div class="card lg:col-span-2"><div class="px-5 pt-5 sm:px-6"><h2 class="card-title">Findings</h2><p class="card-sub">Worst first. Ignore what you have checked.</p></div><div class="mt-3 divide-y divide-ink-50 dark:divide-ink-800">${d.findings.length ? d.findings.map((f) => `<details class="group px-5 py-3 sm:px-6"><summary class="flex cursor-pointer list-none items-center gap-3"><span class="${SEV[f.severity] || "chip-mute"}">${esc(f.severity)}</span><span class="min-w-0 flex-1 truncate text-sm">${esc(f.title)}</span><span class="text-xs text-ink-400">${esc(f.source.replace("quick:", "").replace("deep:", ""))}</span></summary>
          <div class="mt-3 pl-1 text-sm text-ink-500 dark:text-ink-300"><pre class="whitespace-pre-wrap font-mono text-xs">${esc(f.detail || "No detail.")}</pre><div class="mt-3 flex gap-2"><button class="btn-ghost btn-sm" data-ignore="${esc(f.id)}">Ignore from now on</button><span class="text-xs text-ink-400">first seen ${esc(fmt.date(f.first_seen))}</span></div></div></details>`).join("") : empty("No findings", "The quick checks found nothing to report.")}</div></div>
        <div class="flex flex-col gap-4"><div class="card card-pad"><h2 class="card-title">Deep scanners</h2><div class="mt-3 space-y-2">${Object.entries(d.scanners).map(([k, ok]) => { const run = deep.find((x) => x.source === `deep:${k}`); return `<div class="flex items-center justify-between text-sm"><span>${esc(k)}</span>${ok ? `<span class="chip-ok">${run ? esc(run.status) + " · " + fmt.ago(run.ts) : "installed"}</span>` : '<span class="chip-mute">missing</span>'}</div>`; }).join("")}</div>
          ${d.install ? `<p class="mt-4 text-xs text-ink-400">Install the missing ones:</p><button class="mt-1 w-full truncate rounded-xl bg-ink-50 p-2 text-left font-mono text-xs dark:bg-ink-950" data-copy="${esc(d.install)}">${esc(d.install)}</button>` : ""}
          <p class="mt-4 text-xs text-ink-400">Run now (needs root):</p><button class="mt-1 w-full rounded-xl bg-ink-50 p-2 text-left font-mono text-xs dark:bg-ink-950" data-copy="sudo huntaway scan --deep">sudo huntaway scan --deep</button>
          <p class="mt-3 text-xs text-ink-400">Weekly, with a systemd timer:</p><button class="mt-1 w-full rounded-xl bg-ink-50 p-2 text-left font-mono text-xs dark:bg-ink-950" data-copy="sudo huntaway scan schedule">sudo huntaway scan schedule</button></div>
          <div class="card card-pad"><h2 class="card-title">Quick checks</h2><div class="mt-3 space-y-2">${quick.length ? quick.map((x) => `<div class="flex items-center justify-between text-sm"><span>${esc(x.source.replace("quick:", ""))}</span><span class="${x.status === "ok" ? "chip-ok" : x.status === "failed" ? "chip-bad" : "chip-warn"}">${esc(x.status)}</span></div>`).join("") : '<p class="text-sm text-ink-400">Not run yet.</p>'}</div></div></div></div>`;
    $("[data-scan]", el).onclick = async (e) => { e.target.disabled = true; try { await api("security_scan", {}, true); toast("Quick checks done"); } catch (x) { toast(x.message, "bad"); } this.render(el); };
    $$("[data-ignore]", el).forEach((b) => (b.onclick = async () => { await api("finding_ignore", { id: b.dataset.ignore }, true); toast("Ignored"); this.render(el); }));
    $$("[data-copy]", el).forEach((b) => (b.onclick = () => copy(b.dataset.copy)));
  },
};

pages.events = {
  title: "Events", sub: "Everything huntaway did and saw, with the reason",
  kind: "",
  async render(el) {
    const rows = await api("events", { limit: 300, kind: this.kind });
    const kinds = ["", "close", "terminate", "kill", "throttle", "alert", "archive", "judge", "config", "service"];
    el.innerHTML = `<div class="card"><div class="flex flex-wrap gap-2 px-5 pt-5 sm:px-6">${kinds.map((k) => `<button data-k="${k}" class="${k === this.kind ? "chip-tan" : "chip-mute"} cursor-pointer">${k || "all"}</button>`).join("")}</div>
      <ul class="mt-2 divide-y divide-ink-50 px-5 sm:px-6 dark:divide-ink-800">${rows.length ? rows.map(eventRow).join("") : `<li>${empty("No events yet")}</li>`}</ul></div>`;
    $$("[data-k]", el).forEach((b) => (b.onclick = () => { this.kind = b.dataset.k; this.render(el); }));
  },
};

async function getConfig() { const c = await api("config_get"); const flat = {}; for (const [sec, vals] of Object.entries(c.settings)) for (const [k, v] of Object.entries(vals)) flat[`${sec}.${k}`] = v; window.__cfg = flat; return { ...c, flat }; }

pages.rules = {
  title: "Rules and protection", sub: "What huntaway never touches, and the limits you set",
  async render(el) {
    const c = await getConfig();
    const f = c.flat;
    const chips = (key) => `<div class="flex flex-wrap gap-2" data-list="${key}">${f[key].map((v) => `<span class="chip-mute">${esc(v)}<button class="ml-1 text-ink-400 hover:text-red-500" data-rm="${esc(v)}" aria-label="Remove ${esc(v)}">×</button></span>`).join("")}<form class="inline-flex" data-add><input class="input h-7 w-44 py-0 text-xs" placeholder="add and press enter" name="v"></form></div>`;
    el.innerHTML = `<div class="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <div class="card card-pad"><h2 class="card-title">Protected processes</h2><p class="card-sub">Never throttled, stopped or closed. The desktop, sound, terminals, ssh, password managers and call apps are protected already.</p>
        <div class="mt-4"><span class="label">Names</span><div class="mt-2">${chips("protect.names")}</div></div>
        <div class="mt-4"><span class="label">Command line patterns</span><div class="mt-2">${chips("protect.patterns")}</div></div>
        <div class="mt-5 flex items-center justify-between gap-4 rounded-xl bg-ink-50 p-4 dark:bg-ink-850"><div><p class="text-sm font-medium">Protect calls</p><p class="text-xs text-ink-400">Browsers and call apps holding the microphone or camera are left alone.</p></div><button class="switch" role="switch" aria-checked="${f["protect.calls"]}" data-toggle="protect.calls"><span></span></button></div></div>
      <div class="card card-pad"><h2 class="card-title">LLM judge</h2><p class="card-sub">Optional. A model reviews a decision before huntaway ends a process. It can spare a process; it can never end one. Memory emergencies never wait for it.</p>
        <div class="mt-4 seg" data-judge>${["off", "advise", "veto"].map((m) => `<button data-m="${m}" class="${f["judge.mode"] === m ? "on" : ""}">${m}</button>`).join("")}</div>
        <div class="mt-4 grid gap-3 sm:grid-cols-2"><label class="text-sm">Endpoint<input class="input mt-1" data-cfg="judge.base_url" value="${esc(f["judge.base_url"])}"></label><label class="text-sm">Model<input class="input mt-1" data-cfg="judge.model" value="${esc(f["judge.model"])}" placeholder="qwen3:4b"></label>
          <label class="text-sm">API key variable<input class="input mt-1" data-cfg="judge.api_key_env" value="${esc(f["judge.api_key_env"])}" placeholder="empty for a local model"></label><label class="text-sm">Timeout, seconds<input class="input mt-1" type="number" data-cfg="judge.timeout" value="${esc(f["judge.timeout"])}"></label></div>
        <div class="mt-4 flex justify-end"><button class="btn-primary btn-sm" data-save-judge>Save judge settings</button></div></div></div>
      <div class="card mt-4"><div class="flex flex-wrap items-center justify-between gap-3 px-5 pt-5 sm:px-6"><div><h2 class="card-title">Rules</h2><p class="card-sub">Give matching processes their own limits. match is a regular expression over the name or command line.</p></div><button class="btn-ghost btn-sm" data-addrule>Add rule</button></div>
        <div class="mt-3 overflow-x-auto"><table class="tbl" data-rules><thead><tr><th>Name</th><th>Match</th><th>CPU %</th><th>Memory MB</th><th>For seconds</th><th>Action</th><th></th></tr></thead><tbody>${c.rules.map(ruleRow).join("")}</tbody></table>${c.rules.length ? "" : '<p class="px-6 pb-2 text-sm text-ink-400" data-norules>No rules. For example: match <code class="font-mono">node .*vite</code>, memory 4096, terminate.</p>'}</div>
        <div class="flex justify-end px-5 py-4 sm:px-6"><button class="btn-primary btn-sm" data-saverules>Save rules</button></div></div>`;
    const save = async (values) => { try { await api("config_set", { values }, true); toast("Saved. Applied at once."); } catch (e) { toast(e.message, "bad"); } this.render(el); };
    $$("[data-list]", el).forEach((box) => {
      const key = box.dataset.list;
      $$("[data-rm]", box).forEach((b) => (b.onclick = () => save({ [key]: f[key].filter((v) => v !== b.dataset.rm) })));
      $("[data-add]", box).onsubmit = (e) => { e.preventDefault(); const v = e.target.v.value.trim(); if (v) save({ [key]: [...f[key], v] }); };
    });
    $$("[data-toggle]", el).forEach((b) => (b.onclick = () => save({ [b.dataset.toggle]: b.getAttribute("aria-checked") !== "true" })));
    let mode = f["judge.mode"];
    $$("[data-m]", el).forEach((b) => (b.onclick = () => { mode = b.dataset.m; $$("[data-m]", el).forEach((x) => x.classList.toggle("on", x === b)); }));
    $("[data-save-judge]", el).onclick = () => { const values = { "judge.mode": mode }; $$("[data-cfg]", el).forEach((i) => (values[i.dataset.cfg] = i.value)); save(values); };
    $("[data-addrule]", el).onclick = () => { $("[data-norules]", el)?.remove(); $("[data-rules] tbody", el).insertAdjacentHTML("beforeend", ruleRow({ name: "", match: "", cpu_percent: 0, memory_mb: 0, sustain: 30, action: "terminate" })); bindRuleRows(el); };
    bindRuleRows(el);
    $("[data-saverules]", el).onclick = async () => {
      const rules = $$("[data-rules] tbody tr", el).map((tr) => { const g = (n) => $(`[name="${n}"]`, tr).value; return { name: g("name"), match: g("match"), cpu_percent: Number(g("cpu_percent")), memory_mb: Number(g("memory_mb")), sustain: Number(g("sustain")), action: g("action") }; }).filter((r) => r.match);
      try { await api("config_set", { rules }, true); toast("Rules saved"); } catch (e) { toast(e.message, "bad"); }
      this.render(el);
    };
  },
};
const ruleRow = (r) => `<tr><td><input class="input" name="name" value="${esc(r.name)}"></td><td><input class="input font-mono" name="match" value="${esc(r.match)}"></td><td><input class="input w-24" type="number" name="cpu_percent" value="${r.cpu_percent}"></td><td><input class="input w-28" type="number" name="memory_mb" value="${r.memory_mb}"></td><td><input class="input w-24" type="number" name="sustain" value="${r.sustain}"></td>
  <td><select class="input" name="action">${["throttle", "terminate", "kill", "notify"].map((a) => `<option ${a === r.action ? "selected" : ""}>${a}</option>`).join("")}</select></td><td><button class="btn-ghost btn-sm" data-delrule>Remove</button></td></tr>`;
function bindRuleRows(el) { $$("[data-delrule]", el).forEach((b) => (b.onclick = () => b.closest("tr").remove())); }

pages.settings = {
  title: "Settings", sub: "Every option, with its default. Saved to config.toml and applied at once.",
  async render(el) {
    const c = await getConfig();
    const f = c.flat;
    const dirty = {};
    const bySec = {};
    for (const o of c.schema) (bySec[o.section] ||= []).push(o);
    const field = (o) => {
      const v = f[o.key], id = `f-${o.key.replace(".", "-")}`;
      let input;
      if (o.kind === "bool") input = `<button id="${id}" class="switch" role="switch" aria-checked="${v}" data-key="${o.key}" data-kind="bool"><span></span></button>`;
      else if (o.choices.length) input = `<select id="${id}" class="input sm:w-56" data-key="${o.key}">${o.choices.map((ch) => `<option ${ch === v ? "selected" : ""}>${ch}</option>`).join("")}</select>`;
      else if (o.kind === "list") input = `<input id="${id}" class="input sm:w-72" data-key="${o.key}" data-kind="list" value="${esc(v.join(", "))}" placeholder="comma separated">`;
      else input = `<input id="${id}" class="input sm:w-56 ${o.kind === "str" ? "" : "num"}" data-key="${o.key}" data-kind="${o.kind}" ${o.kind !== "str" ? `type="number" step="any" ${o.low != null ? `min="${o.low}"` : ""} ${o.high != null ? `max="${o.high}"` : ""}` : ""} value="${esc(v)}">`;
      const changed = JSON.stringify(v) !== JSON.stringify(o.default);
      return `<div class="flex flex-col gap-3 py-4 sm:flex-row sm:items-start sm:justify-between"><div class="min-w-0 sm:max-w-xl"><label for="${id}" class="text-sm font-medium text-ink-800 dark:text-ink-100">${esc(o.name.replaceAll("_", " "))}</label>${changed ? ' <span class="chip-tan ml-1">changed</span>' : ""}
        <p class="mt-1 text-xs leading-relaxed text-ink-400 dark:text-ink-300">${esc(o.help)} <span class="font-mono">Default ${esc(JSON.stringify(o.default))}.</span></p></div><div class="shrink-0">${input}</div></div>`;
    };
    el.innerHTML = `<div class="grid gap-4">${Object.entries(bySec).map(([sec, opts]) => `<section class="card card-pad"><h2 class="card-title">${esc(sec[0].toUpperCase() + sec.slice(1))}</h2><p class="card-sub">${esc((c.sections || {})[sec] || "")}</p><div class="mt-2 divide-y divide-ink-50 dark:divide-ink-800">${opts.map(field).join("")}</div></section>`).join("")}</div>
      <div class="sticky bottom-4 mt-4 hidden" data-bar><div class="card flex items-center justify-between gap-3 px-5 py-3 shadow-lg"><span class="text-sm"><span data-n>0</span> unsaved changes</span><div class="flex gap-2"><button class="btn-ghost btn-sm" data-undo>Discard</button><button class="btn-primary btn-sm" data-apply>Save and apply</button></div></div></div>
      <p class="mt-4 text-xs text-ink-400">File: <span class="font-mono">${esc(c.path)}</span></p>`;
    const mark = () => { const n = Object.keys(dirty).length; $("[data-bar]", el).classList.toggle("hidden", !n); $("[data-n]", el).textContent = n; };
    $$("[data-key]", el).forEach((i) => {
      if (i.dataset.kind === "bool") i.onclick = () => { const nv = i.getAttribute("aria-checked") !== "true"; i.setAttribute("aria-checked", nv); dirty[i.dataset.key] = nv; mark(); };
      else i.oninput = i.onchange = () => { dirty[i.dataset.key] = i.dataset.kind === "list" ? i.value.split(",").map((x) => x.trim()).filter(Boolean) : i.value; mark(); };
    });
    $("[data-undo]", el).onclick = () => this.render(el);
    $("[data-apply]", el).onclick = async () => { try { await api("config_set", { values: dirty }, true); toast("Saved and applied"); this.render(el); loadService(); } catch (e) { toast(e.message, "bad"); } };
  },
};

pages.integrations = {
  title: "Integrations", sub: "Let your agents check the machine, explain a stopped process and find old sessions",
  async render(el) {
    const d = await api("integrations");
    const snippet = (t) => `<button class="mt-2 block w-full overflow-x-auto rounded-xl bg-ink-50 p-3 text-left font-mono text-xs whitespace-pre text-ink-600 dark:bg-ink-950 dark:text-ink-300" data-copy="${esc(t)}">${esc(t)}</button>`;
    const byH = {};
    for (const s of d.steps) (byH[s.harness] ||= []).push(s);
    el.innerHTML = `<div class="card card-pad"><h2 class="card-title">One command</h2><p class="card-sub">Adds the MCP server and the skill to every harness found on this machine. It uses each harness's own CLI where there is one, and backs up any file it edits.</p>${snippet("huntaway integrate")}
      <p class="mt-3 text-xs text-ink-400">Preview first with <span class="font-mono">huntaway integrate --dry-run</span>. Restart open sessions afterwards.</p></div>
      <div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">${Object.entries(HARNESS).filter(([k]) => ["claude", "codex", "opencode", "gemini", "cursor"].includes(k)).map(([k, [name, color]]) => {
        const steps = byH[k] || [];
        const state = !steps.length ? '<span class="chip-mute">not found</span>' : steps.every((s) => s.done) ? '<span class="chip-ok">connected</span>' : '<span class="chip-warn">not connected</span>';
        return `<div class="card card-pad"><div class="flex items-center justify-between"><div class="flex items-center gap-2"><span class="h-2.5 w-2.5 rounded-full" style="background:${color}"></span><h3 class="card-title">${name}</h3></div>${state}</div>
          ${steps.map((s) => `<p class="mt-2 truncate text-xs text-ink-400"><span class="font-medium text-ink-500 dark:text-ink-300">${esc(s.what)}</span> ${esc(fmt.path(s.target))}${s.note ? ` · ${esc(s.note)}` : ""}</p>`).join("")}
          ${snippet(`huntaway integrate ${k}`)}</div>`; }).join("")}</div>
      <div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div class="card card-pad"><h2 class="card-title">Any MCP client</h2><p class="card-sub">Over stdio:</p>${snippet(JSON.stringify({ mcpServers: { huntaway: { command: d.command[0], args: d.command.slice(1) } } }, null, 2))}
          <p class="mt-3 card-sub">Or over HTTP, with the token as a bearer header:</p>${snippet(d.mcp_http)}</div>
        <div class="card card-pad"><h2 class="card-title">AGENTS.md and CLAUDE.md</h2><p class="card-sub">For tools without MCP, this paragraph teaches an agent the commands.</p>${snippet(d.agents_md.trim())}
          <p class="mt-3 card-sub">Prometheus metrics:</p>${snippet(d.metrics)}</div></div>`;
    $$("[data-copy]", el).forEach((b) => (b.onclick = () => copy(b.dataset.copy)));
  },
};

// Router
let current = null;
let unlisten = null;
async function route() {
  const name = (location.hash.replace(/^#\/?/, "").split(/[/?]/)[0] || "overview");
  const page = pages[name] || pages.overview;
  const id = pages[name] ? name : "overview";
  renderNav(id);
  $("#title").textContent = page.title;
  $("#subtitle").textContent = page.sub || "";
  document.title = `${page.title} · huntaway`;
  if (unlisten) { live.listeners.delete(unlisten); unlisten = null; }
  const el = $("#page");
  if (current !== id) el.innerHTML = `<div class="py-24 text-center text-sm text-ink-300">Loading</div>`;
  current = id;
  closeSidebar();
  try {
    const onLive = await page.render(el);
    if (typeof onLive === "function") { unlisten = onLive; live.listeners.add(onLive); }
  } catch (e) {
    if (!(e instanceof ApiError && e.message === "token required")) el.innerHTML = `<div class="card card-pad text-sm"><p class="font-medium text-red-600">Could not load this page.</p><p class="mt-1 text-ink-400">${esc(e.message)}</p></div>`;
  }
}

function closeSidebar() { $("#sidebar").classList.add("-translate-x-full"); $("#scrim").classList.add("hidden"); }
$("#menu").onclick = () => { $("#sidebar").classList.remove("-translate-x-full"); $("#scrim").classList.remove("hidden"); };
$("#scrim").onclick = closeSidebar;
$("#theme").onclick = () => { const dark = !document.documentElement.classList.contains("dark"); document.documentElement.classList.toggle("dark", dark); store.set("huntaway-theme", dark ? "dark" : "light"); route(); };
$("#search").onsubmit = (e) => { e.preventDefault(); pages.sessions.query = e.target.q.value; location.hash = "#/sessions"; if (current === "sessions") route(); };
window.addEventListener("hashchange", route);

getConfig().catch(() => {});
loadService();
if (!STILL) setInterval(loadService, 30000);
route();
connectLive();
