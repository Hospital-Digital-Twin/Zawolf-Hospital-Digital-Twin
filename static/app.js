/* Hospital Digital Twin dashboard. Every number shown is fetched from the backend; nothing is hard-coded.
   Colour = provenance: observed data (cyan), forecast (violet), simulation (amber), agent output (emerald), alarm (red). */
"use strict";

// ───────────────────────── state & helpers ─────────────────────────
const S = { lang: localStorage.getItem("lang") || "ar", token: sessionStorage.getItem("token"), user: null, meta: null, page: "overview", alertCount: null,
            scn: { unit: "ICU", mult: 12 }, lastRun: null, sim: null, chat: [], health: null, llmOn: false };
const $ = id => document.getElementById(id);
const t = (k, v) => {
  // a missing key in a data-driven family (unit_X, st_X ...) falls back to the raw value coming from the backend
  let s = I18N[S.lang][k] ?? I18N.en[k] ?? (k.includes("_") ? k.split("_").slice(1).join("_") : k);
  if (v) for (const [a, b] of Object.entries(v)) s = s.replaceAll(`{${a}}`, b);
  return s;
};
const loc = () => (S.lang === "ar" ? "ar-EG-u-nu-latn" : "en-GB");
const num = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d }));
const when = iso => iso ? new Date(iso).toLocaleString(loc(), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
const day = iso => iso ? new Date(iso.length === 10 ? iso + "T00:00" : iso).toLocaleDateString(loc(), { day: "numeric", month: "short" }) : "—";
const tickDay = d => new Date(d.length === 10 ? d + "T00:00" : d).toLocaleDateString(loc(), { day: "numeric", month: "numeric" });
/** replaceChildren that skips null/false entries (conditional parts) instead of rendering them as text. */
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));
const NUM = v => h("span", { class: "num" }, v);

/** h(tag, attrs, ...children): text children are always inserted as text nodes, never as HTML. */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}
const ICONS = {
  overview: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z", alerts: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10 21a2 2 0 0 0 4 0",
  beds: "M2 4v16M2 8h18a2 2 0 0 1 2 2v10M2 17h20M6 8v9", er: "M22 12h-4l-3 9L9 3l-3 9H2", staff: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  or: "M12 3v4M5 7h14M7 7v5a5 5 0 0 0 10 0V7M12 17v4M8 21h8", forecast: "M3 17l6-6 4 4 8-8M15 7h6v6", agents: "M12 2l2.4 5.6L20 10l-5.6 2.4L12 18l-2.4-5.6L4 10l5.6-2.4zM19 16l.9 2.1L22 19l-2.1.9L19 22l-.9-2.1L16 19l2.1-.9z",
  simulator: "M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3M7 15h10", decisions: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9",
  assistant: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z", sources: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5zM4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5",
  system: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4", menu: "M4 6h16M4 12h16M4 18h16", search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16M21 21l-4.35-4.35",
  calendar: "M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z", user: "M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20M12 6v6l4 2", clipboard: "M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2M9 2h6v4H9zM9 12h6M9 16h4",
  heart: "M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7z", warn: "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01",
  send: "M22 2L11 13M22 2l-7 20-4-9-9-4z", logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9", refresh: "M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15",
  check: "M20 6L9 17l-5-5", info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20M12 16v-4M12 8h.01", building: "M3 21h18M5 21V7l8-4v18M19 21V11l-6-4M9 9v.01M9 12v.01M9 15v.01M9 18v.01", trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
};
const icon = name => { const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg"), p = document.createElementNS(NS, "path"); for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "ico" })) s.setAttribute(k, v); p.setAttribute("d", ICONS[name] || ICONS.info); s.append(p); return s; };

function toast(msg, tone = "good") { const d = h("div", { class: tone }, msg); $("toast").append(d); setTimeout(() => d.remove(), 4200); }
async function api(path, body) {
  const opts = { headers: {} };
  if (S.token) opts.headers.Authorization = "Bearer " + S.token;
  if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); } catch { throw new Error(t("err_network")); }
  let data = {}; try { data = await res.json(); } catch { /* non-JSON error body */ }
  if (res.status === 401 && S.token && path !== "/api/login") { signOut(); throw new Error(data.error || t("sign_in")); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const q = () => `?as_of=${encodeURIComponent($("asof").value)}`;
const can = p => S.user && S.user.permissions.includes(p);
const unitName = u => t("unit_" + u);
const poolName = key => { const [u, k] = String(key).split(":"); return `${unitName(u)} ${t("kind_" + k)}`; };

// ───────────────────────── building blocks ─────────────────────────
const PROV = { observed: "prov_observed", forecast: "prov_forecast", simulation: "prov_simulation", agent: "prov_agent" };
const provChip = kind => h("span", { class: `chip ${kind}` }, h("i"), t(PROV[kind]));
const pill = (text, tone) => h("span", { class: `chip ${tone || ""}` }, text);
const sev = s => pill(t("sev_" + s), s);
function panel(o, ...body) {
  const foot = [];
  if (o.source) foot.push(h("span", {}, `${t("source")}: ${[].concat(o.source).join(", ")}`));
  if (o.period) foot.push(h("span", {}, `${t("period")}: ${day(o.period.from)} – ${day(o.period.to)}`));
  if (o.unit) foot.push(h("span", {}, `${t("unit")}: ${o.unit}`));
  if (o.note) foot.push(h("span", {}, o.note));
  return h("section", { class: "panel " + (o.cls || "") },
    (o.title || o.prov) && h("header", {}, h("div", {}, h("h3", {}, o.title), o.sub && h("div", { class: "sub" }, o.sub)), h("span", { class: "spacer" }), o.prov && provChip(o.prov), o.right),
    ...body, foot.length > 0 && h("footer", {}, ...foot));
}
const chart = (spec, kind = "line") => { const host = h("div"); queueMicrotask(() => Charts[kind](host, spec)); return host; };
const stat = (label, value, unit, tone) => h("div", { class: "stat" }, h("div", { class: "v", style: tone ? `color:var(--${tone})` : null }, NUM(value), unit && h("span", { class: "unit" }, unit)), h("div", { class: "l" }, label));
const head = (title, sub) => h("div", { class: "page-head" }, h("h1", {}, title), sub && h("p", {}, sub));
const skeleton = (kinds = "kpi,tall") => h("div", { class: "page" }, ...kinds.split(",").map(k => k === "kpi" ? h("div", { class: "kpis" }, [1, 2, 3, 4].map(() => h("div", { class: "skeleton kpi-sk" }))) : h("div", { class: `skeleton ${k === "tall" ? "tall" : ""}` })));
const empty = (msg, ic = "info") => h("div", { class: "empty" }, h("span", { class: "big-ico" }, icon(ic)), h("span", {}, msg));
const errorBox = (msg, retry) => h("div", { class: "errorbox", role: "alert" }, h("span", { class: "big-ico" }, icon("warn")), h("b", {}, t("error")), h("span", {}, msg), retry && h("button", { class: "btn", onclick: retry }, icon("refresh"), t("retry")));
const unavailable = (title, why) => h("div", { class: "unavail" }, h("b", {}, title), h("span", {}, why));
function table(cols, rows) {
  return h("div", { class: "scroll-x" }, h("table", {}, h("thead", {}, h("tr", {}, cols.map(c => h("th", { class: c.n ? "n" : "" }, c.label)))),
    h("tbody", {}, rows.map(r => h("tr", {}, cols.map((c, i) => h("td", { class: c.n ? "n" : "" }, r[i])))))));
}
const alertTitle = a => (S.lang === "ar" ? a.title_ar : a.title) || a.title;
const alertDetail = a => (S.lang === "ar" ? a.detail_ar : a.detail) || a.detail;
const PAGE_OF = { agents: "agents", decisions: "decisions", system: "system", staff: "staff", simulator: "simulator" };
const openLabel = page => t("open_" + (PAGE_OF[page] || "alerts"));
const pages = {};
function setAlertCount(n) { S.alertCount = n; buildUser(); const b = $("nav").querySelector("[data-badge]"); if (b) { b.textContent = n || ""; b.hidden = !n; } }

// ───────────────────────── overview ─────────────────────────
const KPI_STYLE = { beds: ["t-teal", "beds"], patients: ["t-cyan", "user"], icu_er: ["t-emerald", "heart"], staff: ["t-teal", "staff"], surgeries: ["t-cyan", "clipboard"], er_wait: ["t-amber", "clock"], or_util: ["t-emerald", "or"], overtime: ["t-amber", "clock"] };
const UNIT_GROUPS = { all: null, critical: ["ER", "ICU"], inpatient: ["WARD"], operations: ["OR"], support: ["CLINIC", "IMAGING", "LAB"] };

function kpiCard(c) {
  const [tint, ic] = KPI_STYLE[c.key] || ["t-teal", "info"], s = c.sub || {};
  const subs = {
    beds: [t("sub_occupied", { a: s.occupied, b: c.value }), t("sub_pct", { p: num(s.occupancy_pct, 0) })],
    patients: [`ER ${s.per_unit?.ER ?? 0} · ICU ${s.per_unit?.ICU ?? 0} · ${unitName("WARD")} ${s.per_unit?.WARD ?? 0}`],
    icu_er: [`ICU ${c.value}/${s.icu_total}`, `ER ${s.er_occupied}/${s.er_total}`],
    staff: [`${t("nurses")} ${s.nurses}`, `${t("doctors")} ${s.doctors}`],
    surgeries: [t("sub_elective", { n: s.elective })],
  }[c.key] || [];
  const tone = { good: "good", bad: "bad" }[c.tone];
  return h("article", { class: `kpi ${tint}` },
    h("div", { class: "top-row" }, h("span", { class: "ic" }, icon(ic)), h("div", {}, h("div", { class: "label" }, t("kpi_" + c.key)))),
    h("div", { class: "value" }, NUM(num(c.value, c.key === "or_util" || c.key === "er_wait" ? 1 : 0)), h("span", { class: "unit" }, t("u_" + c.unit))),
    h("div", { class: "sub" }, subs.map(x => h("span", {}, x)), c.delta_pct != null && h("span", { class: `delta ${tone || "flat"}` }, `${c.delta_pct > 0 ? "▲" : c.delta_pct < 0 ? "▼" : "•"} ${num(Math.abs(c.delta_pct))}%`)),
    Charts.spark(c.spark, `var(--${tint === "t-amber" ? "sim" : tint === "t-cyan" ? "observed" : tint === "t-emerald" ? "agent" : "teal"})`),
    h("div", { class: "src" }, `${t("source")}: ${c.source}`));
}

function zoneSeverity(alerts) {
  const rank = { critical: 3, high: 2, medium: 1 }, by = {};
  for (const a of alerts) {
    const units = a.evidence.units || (a.evidence.pool ? [a.evidence.pool.split(":")[0]] : []);
    for (const u of units) if ((rank[a.severity] || 0) > (rank[by[u]] || 0)) by[u] = a.severity;
  }
  return by;
}
function twinPanel(m, alerts, onZone) {
  const sevBy = zoneSeverity(alerts), by = Object.fromEntries(m.departments.map(d => [d.type, d]));
  let filter = "all";
  const grid = h("div", { class: "twin" });
  const zone = (type, cls) => {
    const d = by[type]; if (!d) return null;
    const s = sevBy[type], state = s === "critical" ? "s-critical" : s === "high" ? "s-high" : "s-ok";
    const link = { ER: "er", ICU: "beds", WARD: "beds", OR: "or", CLINIC: "er", IMAGING: "er", LAB: "er" }[type];
    const body = d.beds ? h("div", { class: "beds" }, d.beds.map(b => h("span", { class: "bed" + (b.occupied ? " occ" : ""), title: `${b.id} · ${b.room} · ${b.occupied ? t("bed_occupied") : t("bed_free")}` })))
      : d.rooms ? h("div", { class: "rooms" }, d.rooms.map(r => h("div", { class: "room-row" }, h("span", {}, r.name.replace(/^Operating Room /, "OR ")), h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, 100 * r.booked_min_on_day / r.capacity_min)}%;background:rgb(var(--tint-rgb))` })), NUM(num(100 * r.booked_min_on_day / r.capacity_min, 0) + "%"))))
      : h("div", { class: "meta" }, h("span", {}, `${t("arrivals")}: `, NUM(num(d.arrivals, 0))), h("span", {}, `${t("wait_avg")}: `, NUM(num(d.avg_wait_min))));
    return h("button", { class: `zone ${state} ${cls || ""}`, "data-type": type, onclick: () => onZone(link), "aria-label": `${S.lang === "ar" ? d.name_ar : d.name}` },
      h("div", { class: "zh" }, h("b", {}, S.lang === "ar" ? d.name_ar : d.name), h("span", { class: "count" }, d.beds ? NUM(`${d.occupied}/${d.beds_total}`) : d.rooms ? NUM(`${d.rooms.length}`) : null)),
      body, h("div", { class: "meta" }, d.nurses != null && h("span", {}, `${t("nurses")} ${d.nurses}`), d.doctors != null && h("span", {}, `${t("doctors")} ${d.doctors}`), s && sev(s)));
  };
  const draw = () => {
    fill(grid, zone("ER"), zone("ICU"), zone("WARD"), zone("OR", "wide"), h("div", { class: "stack-zones" }, zone("CLINIC"), zone("IMAGING"), zone("LAB")));
    const allowed = UNIT_GROUPS[filter];
    grid.querySelectorAll("[data-type]").forEach(z => z.classList.toggle("dim", !!allowed && !allowed.includes(z.dataset.type)));
  };
  const seg = h("div", { class: "seg", role: "group" }, Object.keys(UNIT_GROUPS).map(k => h("button", { type: "button", "aria-pressed": String(k === filter), onclick: e => { filter = k; seg.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b === e.currentTarget))); draw(); } }, t("grp2_" + k))));
  queueMicrotask(draw);
  return panel({ title: t("twin_title"), sub: t("twin_sub", { when: when(m.as_of) }), prov: "observed", right: seg, source: m.sources, cls: "twin-panel" }, grid,
    h("div", { class: "twin-legend" }, h("span", {}, h("i", { style: "background:var(--observed)" }), t("bed_occupied")), h("span", {}, h("i", { style: "border:1.5px solid var(--observed)" }), t("bed_free")),
      h("span", {}, h("i", { style: "background:var(--alarm)" }), t("sev_critical")), h("span", {}, h("i", { style: "background:var(--sim)" }), t("sev_high")), h("span", {}, h("i", { style: "background:var(--agent)" }), t("zone_ok")), h("span", { class: "faint" }, t("schematic"))));
}
function alertCard(a) {
  return h("button", { class: `alert ${a.severity}`, onclick: () => go(PAGE_OF[a.page] || "alerts") },
    h("div", { class: "row" }, sev(a.severity), h("span", { class: "chip" }, t("src_" + a.source))), h("b", {}, alertTitle(a)), h("p", {}, alertDetail(a)));
}
function pressureView(rows) {
  const max = Math.max(10, ...rows.map(r => Math.abs(r.delta_pct ?? 0)));
  return h("div", { class: "grid", style: "gap:10px" }, rows.map(r => {
    const d = r.delta_pct, w = d == null ? 0 : 50 * Math.abs(d) / max;
    return h("div", { class: "div-row" }, h("span", {}, (S.lang === "ar" ? r.name_ar : r.name), h("div", { class: "faint small" }, `${t("metric_" + r.metric)} (${t("u_" + r.unit)})`)),
      h("div", { class: "div-bar" }, d != null && h("i", { style: `${d >= 0 ? "left:50%" : `left:${50 - w}%`};width:${w}%;background:${d >= 0 ? "var(--sim)" : "var(--observed)"}` })),
      h("span", { class: "num" }, d == null ? "—" : `${d > 0 ? "+" : ""}${num(d, 0)}%`));
  }));
}
pages.overview = async root => {
  const [o, al, m] = await Promise.all([api("/api/overview" + q()), api("/api/alerts" + q()), api("/api/twin" + q())]);
  setAlertCount(al.needs_attention);
  const shown = al.alerts.filter(a => a.severity !== "info").concat(al.alerts.filter(a => a.severity === "info")).slice(0, 5);
  const used = o.bed_use, pct = used.used + used.available ? 100 * used.used / (used.used + used.available) : 0;
  const perUnit = o.cards.find(c => c.key === "patients").sub.per_unit, maxUnit = Math.max(1, ...Object.values(perUnit));
  const perf = o.performance;
  fill(root, h("div", { class: "page" },
    h("div", {}, h("p", { class: "mute" }, t("replay_note", { when: when(o.as_of) })), h("h1", { class: "headline" }, al.needs_attention ? t("headline_n", { n: al.needs_attention }) : t("headline_ok"))),
    h("div", { class: "kpis" }, o.cards.map(kpiCard)),
    h("div", { class: "twin-row" },
      twinPanel(m, al.alerts, go),
      panel({ title: t("critical_alerts"), sub: t("n_items", { n: al.alerts.length }), right: h("button", { class: "link", onclick: () => go("alerts") }, t("view_all")) },
        h("div", { class: "alerts" }, shown.length ? shown.map(alertCard) : empty(t("nothing_urgent"), "check")))),
    h("div", { class: "grid g3" },
      panel({ title: t("patient_flow"), sub: t("patient_flow_sub"), prov: "observed", unit: t("u_patients/hour"), source: "visits", period: o.er_hourly.window },
        chart({ labels: [...Array(24).keys()], tickFmt: x => String(x).padStart(2, "0"), tipFmt: x => String(x).padStart(2, "0") + ":00", unit: t("u_patients/hour"), yMin: 0, fmt: v => num(v, 2),
          series: [{ name: t("arrivals"), values: o.er_hourly.values, color: Charts.COLOR.observed, area: true }] })),
      panel({ title: t("bed_use"), sub: t("bed_use_sub"), prov: "observed", unit: t("u_beds"), source: ["beds", "bed_stays"] },
        h("div", { class: "donut-wrap" }, Charts.ring(used.used, used.used + used.available, "var(--teal)", num(pct, 0) + "%"),
          h("div", { class: "legend-list" }, h("div", {}, h("i", { style: "background:var(--teal)" }), t("bed_occupied"), h("b", {}, NUM(used.used))), h("div", {}, h("i", { style: "background:var(--raised);border:1px solid var(--faint)" }), t("bed_free"), h("b", {}, NUM(used.available))),
            h("div", {}, h("i", { style: "background:var(--faint)" }), t("bed_closed"), h("b", {}, NUM(used.closed)))))),
      panel({ title: t("patients_by_unit"), sub: t("patients_by_unit_sub"), prov: "observed", unit: t("u_patients"), source: "bed_stays" },
        h("div", { class: "grid", style: "gap:12px" }, Object.entries(perUnit).map(([u, n]) => h("div", { class: "bar-row" }, h("span", {}, unitName(u)), h("div", { class: "bar" }, h("i", { style: `width:${100 * n / maxUnit}%;background:var(--observed)` })), NUM(n)))))),
    h("div", { class: "grid g3" },
      panel({ title: t("c_occ"), prov: "observed", unit: "%", source: "daily_metrics", period: o.period, sub: t("occ_now", { used: o.inpatient.occupied_now, total: o.inpatient.beds_total, pct: num(o.inpatient.occupancy_now_pct, 0) }) },
        chart({ labels: o.occupancy.dates, tickFmt: tickDay, unit: "%", yMin: 0, series: [{ name: unitName("ICU"), values: o.occupancy.icu_pct, color: Charts.COLOR.observed, area: true }, { name: unitName("WARD"), values: o.occupancy.ward_pct, color: "var(--teal)" }] })),
      panel({ title: t("forecast_title"), sub: t("forecast_next"), prov: "forecast", unit: "%", source: "daily_metrics", note: t("fc_method") },
        chart({ labels: o.forecast_occupancy.WARD.map(p => p.date), tickFmt: tickDay, unit: "%", yMin: 0, series: [
          { name: unitName("WARD"), values: o.forecast_occupancy.WARD.map(p => p.predicted), color: Charts.COLOR.forecast, band: [o.forecast_occupancy.WARD.map(p => p.lower), o.forecast_occupancy.WARD.map(p => p.upper)] },
          { name: unitName("ICU"), values: o.forecast_occupancy.ICU.map(p => p.predicted), color: "var(--observed)", dashed: true }] })),
      panel({ title: t("dept_pressure"), sub: t("dept_pressure_sub"), prov: "observed", source: "daily_metrics", period: o.period }, pressureView(o.pressure))),
    panel({ title: t("performance"), sub: t("performance_sub"), prov: "observed", source: ["daily_metrics", "visits", "surgeries"] },
      h("div", { class: "perf" }, [[t("pi_wait"), perf.er_wait_min, t("u_min")], [t("pi_lwbs"), perf.lwbs_pct, "%"], [t("pi_delay"), perf.or_delay_pct, "%"], [t("pi_cancel"), perf.or_cancel_pct, "%"]].map(([l, v, u]) => h("div", {}, h("div", { class: "v" }, NUM(num(v)), h("span", { class: "unit mute small", style: "margin-inline-start:6px" }, u)), h("div", { class: "l" }, l)))))));
};

// ───────────────────────── alerts & recommendations ─────────────────────────
pages.alerts = async root => {
  const al = await api("/api/alerts" + q()); setAlertCount(al.needs_attention);
  const group = s => al.alerts.filter(a => a.severity === s);
  fill(root, h("div", { class: "page" }, head(t("ttl_alerts"), t("alerts_sub")),
    al.needs_attention === 0 && h("div", { class: "callout good" }, icon("check"), t("nothing_urgent")),
    ...["critical", "high", "medium", "info"].map(s => group(s).length ? panel({ title: t("sev_" + s), sub: t("n_items", { n: group(s).length }) }, h("div", { class: "alerts" }, group(s).map(alertCard))) : null),
    panel({ title: t("recommendations"), sub: t("recommendations_sub"), prov: "agent", right: h("button", { class: "btn small", onclick: () => go("agents") }, t("open_agents")) },
      al.recommendations.length ? h("div", { class: "agent-grid" }, al.recommendations.map(r => h("div", { class: "agent-tile" }, h("b", {}, t("agent_" + r.agent)), h("div", { class: "small" }, r.summary), h("div", { class: "small faint" }, r.rationale.join(" · "))))) : empty(t("no_actions"), "check"),
      al.rejected.length > 0 && h("details", {}, h("summary", { class: "mute" }, t("rejected_n", { n: al.rejected.length })), h("ul", { class: "plain small" }, al.rejected.map(r => h("li", {}, h("b", {}, t("agent_" + r.agent)), ": ", r.reasons.join(" · ")))))
    )));
};

// ───────────────────────── beds & ICU ─────────────────────────
function forecastChart(history, hDates, fc, unit, title) {
  const labels = [...hDates, ...fc.map(p => p.date)], n = hDates.length;
  return { labels, tickFmt: tickDay, unit, yMin: 0, title, marker: { index: n, label: t("replay") }, series: [
    { name: t("observed"), color: Charts.COLOR.observed, values: [...history, ...fc.map(p => p.actual)], area: true },
    { name: t("forecast"), color: Charts.COLOR.forecast, dashed: true, values: [...history.map(() => null), ...fc.map(p => p.predicted)], band: [[...history.map(() => null), ...fc.map(p => p.lower)], [...history.map(() => null), ...fc.map(p => p.upper)]] }], bandLabel: t("range_1sd") };
}
const histDates = to => { const out = [], end = new Date(to + "T00:00"); for (let i = 13; i >= 0; i--) { const d = new Date(end); d.setDate(d.getDate() - i); out.push(d.toISOString().slice(0, 10)); } return out; };
const unitInstrument = (u, d, tint) => panel({ title: unitName(u), prov: "observed", source: ["beds", "bed_stays", "staff_shifts"], note: t("at_moment", { when: when($("asof").value) }) },
  h("div", { class: "range" }, Charts.ring(d.occupied_now, d.beds_total, tint, d.occupancy_now_pct == null ? "—" : num(d.occupancy_now_pct, 0) + "%"),
    h("div", { class: "grid", style: "gap:10px" }, stat(t("beds_occupied"), `${d.occupied_now} / ${d.beds_total}`), stat(t("nurses_on_shift"), d.nurses_on_shift ?? "—"), stat(t("beds_per_nurse"), d.beds_occupied_per_nurse ?? "—"))),
  h("p", { class: "small faint" }, t("per_nurse_note")));
pages.beds = async root => {
  const [c, al] = await Promise.all([api("/api/capacity" + q()), api("/api/alerts" + q())]);
  const U = c.units, hd = histDates(c.history_period.to);
  const capRow = u => { const peakPct = U[u].peak_forecast_upper_pct, demand = peakPct == null ? null : peakPct / 100 * U[u].beds_total, short = demand == null ? null : Math.max(0, Math.ceil(demand - 1e-9) - U[u].beds_total);
    return [unitName(u), NUM(U[u].beds_total), NUM(U[u].occupied_now), NUM(num(demand)), NUM(short == null ? "—" : short)]; };
  const fc = u => panel({ title: t("occ_forecast", { u: unitName(u) }), prov: "forecast", unit: "%", source: "daily_metrics", sub: t("fc_sub"), right: U[u].backtest.wape != null ? pill(`WAPE ${num(U[u].backtest.wape, 2)}`, "info") : null, note: t("fc_method") },
    chart(forecastChart(U[u].occupancy_history_pct, hd, U[u].forecast_pct, "%", u)));
  fill(root, h("div", { class: "page" }, head(t("nav_beds"), t("beds_sub")),
    h("div", { class: "grid g3" }, ["ICU", "WARD", "ER"].map((u, i) => unitInstrument(u, U[u], ["var(--agent)", "var(--teal)", "var(--observed)"][i]))),
    panel({ title: t("capacity_gap"), sub: t("capacity_gap_sub"), prov: "forecast", source: ["beds", "daily_metrics"], note: t("gap_note") },
      table([{ label: t("department") }, { label: t("beds_total"), n: 1 }, { label: t("beds_occupied"), n: 1 }, { label: t("peak_demand_beds"), n: 1 }, { label: t("shortfall_h"), n: 1 }], ["ICU", "WARD"].map(capRow)),
      Object.keys(al.unmet).length > 0 && h("div", { class: "callout warn" }, icon("warn"), t("unmet_after", { list: Object.entries(al.unmet).map(([p, n]) => `${poolName(p)}: ${n}`).join("، ") }))),
    h("div", { class: "grid g2" }, fc("ICU"), fc("WARD"))));
};

// ───────────────────────── ER & departments ─────────────────────────
pages.er = async root => {
  const [c, o] = await Promise.all([api("/api/capacity" + q()), api("/api/overview" + q())]);
  const f = c.units.ER.flow, hd = histDates(c.history_period.to), hours = [...Array(24).keys()];
  const mix = (obj, label) => Object.entries(obj).sort((a, b) => b[1] - a[1]).map(([k, v]) => h("div", { class: "bar-row" }, h("span", { class: "small" }, label(k)), h("div", { class: "bar" }, h("i", { style: `width:${100 * v / f.visits_in_window}%;background:var(--teal)` })), NUM(num(100 * v / f.visits_in_window, 1) + "%")));
  fill(root, h("div", { class: "page" }, head(t("nav_er"), t("er_sub")),
    h("div", { class: "grid g2" },
      panel({ title: t("er_by_hour"), prov: "observed", unit: t("u_patients/hour"), source: "visits", period: f.window, sub: t("er_by_hour_sub") },
        chart({ labels: hours, tickFmt: x => String(x).padStart(2, "0") + ":00", tipFmt: x => String(x).padStart(2, "0") + ":00", unit: t("u_patients/hour"), series: [{ name: t("arrivals"), values: f.arrivals_by_hour, color: Charts.COLOR.observed }], fmt: v => num(v, 2) }, "bars")),
      panel({ title: t("er_wait"), prov: "observed", unit: t("u_min"), source: "daily_metrics", period: c.history_period },
        chart({ labels: hd, tickFmt: tickDay, unit: t("u_min"), yMin: 0, series: [{ name: t("wait_avg"), values: f.wait_avg_history, color: Charts.COLOR.sim, area: true }, { name: t("wait_p90"), values: f.wait_p90_history, color: Charts.COLOR.alarm, dashed: true }] }))),
    h("div", { class: "grid g2" },
      panel({ title: t("er_outcomes"), prov: "observed", unit: t("u_pct_visits"), source: "visits", period: f.window, sub: t("lwbs", { v: num(f.left_without_being_seen_pct) }) }, mix(f.dispositions, k => t("disp_" + k))),
      panel({ title: t("er_acuity"), prov: "observed", unit: t("u_pct_visits"), source: "visits", period: f.window, sub: t("acuity_note") }, mix(f.acuity, k => `${t("acuity")} ${k}`))),
    panel({ title: t("dept_pressure"), sub: t("dept_pressure_sub"), prov: "observed", source: "daily_metrics", period: o.period }, pressureView(o.pressure)),
    panel({ title: t("departments"), sub: t("latest_day", { d: day(o.latest_day) }), prov: "observed", source: "daily_metrics" },
      table([{ label: t("department") }, { label: t("arrivals"), n: 1 }, { label: t("occupancy") + " %", n: 1 }, { label: `${t("wait_avg")} (${t("u_min")})`, n: 1 }, { label: `${t("overtime")} (${t("u_hours")})`, n: 1 }, { label: t("absences"), n: 1 }],
        o.departments.map(d => [S.lang === "ar" ? d.name_ar : d.name, NUM(num(d.arrivals, 0)), NUM(num(d.occupancy_pct, 0)), NUM(num(d.avg_wait_min)), NUM(num(d.overtime_hours)), NUM(num(d.absences, 0))])))));
};

// ───────────────────────── staff & nursing ─────────────────────────
pages.staff = async root => {
  const s = await api("/api/staff" + q()), d = s.shifts_per_day.dates;
  const med = [...s.shifts_per_day.values].sort((a, b) => a - b)[Math.floor(d.length / 2)];
  const odd = i => s.shifts_per_day.values[i] < 0.5 * med || s.shifts_per_day.values[i] > 2 * med;
  const units = [...new Set(s.on_shift_now.map(x => x.unit))];
  const cnt = (u, k) => s.on_shift_now.find(x => x.unit === u && x.kind === k)?.count ?? 0;
  fill(root, h("div", { class: "page" }, head(t("nav_staff"), t("staff_sub")),
    h("div", { class: "grid g2" },
      panel({ title: t("on_shift"), prov: "observed", source: ["staff_shifts", "staff"], note: t("at_moment", { when: when($("asof").value) }) },
        table([{ label: t("department") }, { label: t("nurses"), n: 1 }, { label: t("doctors"), n: 1 }], units.map(u => [unitName(u), NUM(cnt(u, "nurses")), NUM(cnt(u, "doctors"))]))),
      panel({ title: t("beds_per_nurse"), prov: "observed", source: ["bed_stays", "staff_shifts"], sub: t("per_nurse_note") }, h("div", { class: "grid g3" }, ["ER", "ICU", "WARD"].map(u => stat(unitName(u), s.observed_beds_per_nurse[u] ?? "—"))))),
    h("div", { class: "grid g2" },
      panel({ title: t("shifts_day"), prov: "observed", unit: t("u_shifts"), source: "staff_shifts", period: s.history_period, sub: t("coverage_note", { m: med }) },
        chart({ labels: d, tickFmt: tickDay, unit: t("u_shifts"), series: [{ name: t("shifts"), values: s.shifts_per_day.values, color: Charts.COLOR.observed }, { name: t("absent"), values: s.absent_shifts_per_day.values, color: Charts.COLOR.alarm }], colors: (si, i) => (si === 0 && odd(i) ? "var(--sim)" : null) }, "bars")),
      panel({ title: t("overtime_title"), prov: "observed", unit: `${t("u_hours")}`, source: ["daily_metrics", "financial_daily"], period: s.history_period },
        chart({ labels: d, tickFmt: tickDay, unit: t("u_hours"), yMin: 0, series: [{ name: t("overtime_h"), values: s.overtime_hours.values, color: Charts.COLOR.sim, area: true }] }),
        h("p", { class: "small mute" }, t("overtime_cost", { v: num(s.overtime_cost_egp.values.reduce((a, b) => a + b, 0), 0) })))),
    h("div", { class: "grid g2" },
      panel({ title: t("workload"), prov: "observed", unit: t("u_staff"), source: ["staff", "staff_shifts"], sub: t("workload_sub") },
        chart({ labels: s.workload_distribution.buckets.map(b => b + "%"), unit: t("u_staff"), series: [{ name: t("staff_count"), values: s.workload_distribution.staff, color: Charts.COLOR.observed }], tickFmt: String }, "bars")),
      panel({ title: t("workload_unit"), prov: "observed", source: ["staff", "staff_shifts"] },
        table([{ label: t("department") }, { label: `${t("mean_load")} %`, n: 1 }, { label: t("staff_count"), n: 1 }, { label: "≥ 90%", n: 1 }], s.workload_by_unit.map(u => [unitName(u.unit), NUM(num(u.mean_pct)), NUM(u.staff), NUM(u.at_or_above_90_pct)]))))));
};

// ───────────────────────── operating rooms ─────────────────────────
pages.or = async root => {
  const o = await api("/api/or" + q()), sc = o.status_counts;
  const order = ["COMPLETED", "SCHEDULED", "DELAYED", "CANCELLED"], colors = { COMPLETED: "var(--agent)", SCHEDULED: "var(--observed)", DELAYED: "var(--sim)", CANCELLED: "var(--alarm)" };
  const kc = (tint, ic, label, v, unit) => h("article", { class: `kpi ${tint}` }, h("div", { class: "top-row" }, h("span", { class: "ic" }, icon(ic)), h("div", { class: "label" }, label)), h("div", { class: "value" }, NUM(v), h("span", { class: "unit" }, unit)));
  fill(root, h("div", { class: "page" }, head(t("nav_or"), t("or_sub")),
    h("div", { class: "kpis" }, kc("t-amber", "clock", t("delay_rate"), num(o.delay_rate_pct), "%"), kc("t-red", "warn", t("cancel_rate"), num(o.cancel_rate_pct), "%"),
      kc("t-teal", "clipboard", t("cancelled_margin"), num(o.cancelled_expected_margin_egp, 0), "EGP"), kc("t-cyan", "or", t("surgeries_window"), o.surgeries_in_window, t("u_cases"))),
    h("div", { class: "grid g2" },
      panel({ title: t("or_util"), prov: "observed", unit: "%", source: "daily_metrics", period: o.history_period }, chart({ labels: o.dates, tickFmt: tickDay, unit: "%", yMin: 0, series: [{ name: t("or_util"), values: o.utilisation_history_pct, color: Charts.COLOR.observed, area: true }] })),
      panel({ title: t("or_status"), prov: "observed", unit: t("u_cases"), source: "surgeries", period: o.history_period }, chart({ labels: order.map(k => t("st_" + k)), unit: t("u_cases"), series: [{ name: t("u_cases"), values: order.map(k => sc[k] || 0), color: Charts.COLOR.observed, noLegend: true }], colors: (si, i) => colors[order[i]] }, "bars"))),
    h("div", { class: "grid g2" },
      panel({ title: t("or_rooms"), prov: "observed", unit: t("u_pct_room_time"), source: ["surgeries", "operating_rooms"], period: o.history_period, sub: t("or_rooms_sub") },
        h("div", { class: "grid", style: "gap:10px" }, o.by_room.map(r => h("div", { class: "bar-row" }, h("span", { class: "small" }, r.name), h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, r.planned_utilisation_pct)}%;background:var(--teal)` })), NUM(num(r.planned_utilisation_pct) + "%"))))),
      panel({ title: t("or_upcoming"), prov: "observed", unit: t("u_pct_room_time"), source: "surgeries", note: t("no_calendar"), sub: t("or_upcoming_sub") },
        o.upcoming.dates.length ? [chart({ labels: o.upcoming.dates, tickFmt: tickDay, unit: "%", series: [{ name: t("booked_load"), values: o.upcoming.load_pct, color: Charts.COLOR.observed }] }, "bars"),
          table([{ label: t("date") }, { label: t("bookings"), n: 1 }, { label: t("movable"), n: 1 }], o.upcoming.dates.map((d, i) => [day(d), NUM(o.upcoming.bookings[i]), NUM(o.upcoming.movable_elective[i])]))] : empty(t("no_upcoming"), "or")))));
};

// ───────────────────────── forecasts ─────────────────────────
pages.forecast = async root => {
  const f = await api("/api/forecasts" + q());
  const methodTable = s => { const r = s.rolling, best = r.best_by_mae;
    return table([{ label: t("method") }, { label: t("n_points"), n: 1 }, { label: `${t("mae")} (${s.unit})`, n: 1 }, { label: "WAPE", n: 1 }, { label: t("skill"), n: 1 }],
      Object.entries(r.methods).map(([k, m]) => [h("span", {}, t("m_" + k), k === best && h("span", { class: "chip good", style: "margin-inline-start:8px" }, t("best"))), NUM(m.n), NUM(num(m.mae, 2)), NUM(num(m.wape, 2)), NUM(m.skill_vs_seasonal_naive == null ? "—" : num(m.skill_vs_seasonal_naive, 2))])); };
  fill(root, h("div", { class: "page" }, head(t("nav_forecast"), t("forecast_sub")), h("div", { class: "callout warn" }, icon("warn"), t("fc_warning")),
    h("div", { class: "grid g2" }, f.series.map(s => panel({ title: t("fc_" + s.key), prov: "forecast", unit: s.unit, source: s.table, sub: t("fc_sub"),
      right: s.backtest.wape != null ? pill(`WAPE ${num(s.backtest.wape, 2)}`, "info") : null, note: t("backtest_days", { n: s.backtest.days }) },
      chart(forecastChart(s.history.values, s.history.dates, s.forecast, s.unit, s.title)),
      h("h4", {}, t("rolling_title")), h("div", { class: "small mute" }, t("rolling_sub", { a: day(s.rolling.window.from), b: day(s.rolling.window.to), h: s.rolling.horizon_days })), methodTable(s),
      s.rolling.methods.weekday_mean.mean_actual != null && s.rolling.methods.weekday_mean.mean_actual < 15 && h("div", { class: "small faint" }, t("low_volume", { v: num(s.rolling.methods.weekday_mean.mean_actual, 1) })))))));
};

// ───────────────────────── agents ─────────────────────────
const AGENTS = ["er", "bed", "staff", "or", "appointment", "resource", "finance"];
function scenarioControls(onRun, runLabel, extra) {
  const unit = h("select", { id: "unit", "aria-label": t("stress_unit") }, ["ICU", "WARD"].map(u => h("option", { value: u }, unitName(u)))); unit.value = S.scn.unit; unit.addEventListener("change", () => S.scn.unit = unit.value);
  const out = h("b", { class: "num" }, S.scn.mult + "×");
  const mult = h("input", { type: "range", min: 1, max: 20, step: 0.5, value: S.scn.mult, "aria-label": t("multiplier") });
  mult.addEventListener("input", () => { S.scn.mult = Number(mult.value); out.textContent = mult.value + "×"; });
  const presets = [1, 2, 5, 12].map(v => h("button", { class: "btn ghost small", type: "button", onclick: () => { S.scn.mult = v; mult.value = v; out.textContent = v + "×"; } }, v + "×"));
  const btn = h("button", { class: "btn primary", disabled: !can("run"), title: can("run") ? "" : t("no_permission"), onclick: async () => { btn.disabled = true; try { await onRun(); } catch (e) { toast(e.message, "bad"); } finally { btn.disabled = !can("run"); } } }, runLabel);
  return h("div", { class: "panel" }, h("div", { class: "range", style: "flex-wrap:wrap" }, h("label", { class: "field" }, t("stress_unit"), unit), h("label", { class: "field" }, t("multiplier"), h("div", { class: "range" }, mult, out)), h("div", { class: "range" }, presets), btn, extra), h("p", { class: "small mute" }, t("stress_note")));
}
function cardView(d) {
  const c = d.card, unmet = Object.entries(c.unmet || {});
  const m = (l, v, tone) => h("div", { class: "stat" }, h("div", { class: "v", style: tone ? `color:var(--${tone})` : null }, v), h("div", { class: "l" }, l));
  const na = () => h("span", { class: "mute", style: "font-size:16px" }, t("unavailable"));
  return panel({ title: t("exec_card"), prov: "agent", sub: d.id, right: unmet.length ? sev("critical") : null },
    h("div", { class: "grid g4" }, m(t("bottleneck"), c.critical_bottleneck ? poolName(c.critical_bottleneck) : t("none_found"), c.critical_bottleneck ? "sim" : "agent"),
      m(t("demand_vs_now"), c.expected_demand_change_pct == null ? "—" : NUM(`${c.expected_demand_change_pct > 0 ? "+" : ""}${num(c.expected_demand_change_pct)}%`)),
      m(t("wait_saved"), c.waiting_minutes_saved == null ? na() : NUM(num(c.waiting_minutes_saved))), m(t("confidence"), c.confidence == null ? na() : NUM(num(c.confidence, 2)))),
    unmet.length > 0 && h("div", { class: "callout warn" }, icon("warn"), t("unmet_after", { list: unmet.map(([p, n]) => `${poolName(p)}: ${n}`).join("، ") })),
    h("p", {}, h("b", {}, t("summary") + ": "), d.explanation.text, " ", h("span", { class: "chip" }, d.explanation.source === "llm" ? t("by_llm") : t("by_template"))),
    h("details", {}, h("summary", { class: "mute" }, t("assumptions")), h("ul", { class: "plain small" }, c.assumptions.map(a => h("li", {}, a)))),
    c.unverified.length > 0 && h("details", {}, h("summary", { class: "mute" }, t("not_verified")), h("ul", { class: "plain small" }, c.unverified.map(a => h("li", {}, a)))));
}
const actionText = a => a.pool ? `${poolName(a.pool)} ${a.delta > 0 ? "+" : ""}${a.delta}` : a.booking_id ? `${a.booking_id}: ${a.from_slot.resource_id} ${day(a.from_slot.start)} → ${a.to_slot.resource_id} ${day(a.to_slot.start)}` : `${t("alert")}: ${a.detail}`;
const needsPolicy = d => d.rejected.some(r => r.reasons.some(x => x.includes("unverifiable")));
const policyHint = d => needsPolicy(d) ? h("div", { class: "callout warn" }, icon("warn"), h("span", {}, t("policy_hint"), " ", h("button", { class: "link", onclick: () => go("system") }, t("open_system")))) : null;
function proposalsView(d) {
  const row = (p, why, tone) => h("tr", {}, h("td", {}, h("b", {}, t("agent_" + p.agent))), h("td", {}, p.actions.map(a => h("div", {}, actionText(a)))), h("td", { class: `small ${tone || "mute"}` }, why));
  return [policyHint(d), panel({ title: t("accepted"), prov: "agent", sub: t("accepted_sub") }, d.accepted.length ? h("div", { class: "scroll-x" }, h("table", {}, h("tbody", {}, d.accepted.map(p => row(p, p.rationale.join(" · "))))))
      : empty(t("no_actions"), "check"), d.accepted.length > 0 && h("details", {}, h("summary", { class: "mute" }, t("rollback_plan")), h("ul", { class: "plain small" }, d.rollback_actions.map(a => h("li", {}, actionText(a)))))),
    d.rejected.length > 0 && panel({ title: t("rejected"), sub: t("rejected_sub") }, h("div", { class: "scroll-x" }, h("table", {}, h("tbody", {}, d.rejected.map(r => row(r.proposal, r.reasons.join(" · "), "bad")))))),
    panel({ title: t("agent_reports"), prov: "agent" }, h("div", { class: "agent-grid" }, AGENTS.map(a => {
      const r = d.reports.find(x => x.agent === a); if (!r) return null;
      const state = r.error ? [t("failed"), "bad"] : r.proposals.length ? [t("proposed", { n: r.proposals.length }), "good"] : [t("no_proposal"), "info"];
      return h("div", { class: "agent-tile" }, h("div", { class: "range" }, h("b", {}, t("agent_" + a)), h("span", { class: "spacer" }), pill(state[0], state[1])), h("div", { class: "small mute" }, t("agent_role_" + a)),
        ...[...new Set(r.notes)].slice(0, 2).map(n => h("div", { class: "small faint" }, n)), r.error && h("div", { class: "small bad" }, r.error)); }))),
    d.policy_passages.length > 0 && panel({ title: t("policy_refs"), sub: t("policy_refs_sub") }, h("ul", { class: "plain small" }, d.policy_passages.map(p => h("li", {}, h("b", {}, p.source), ": ", p.text.slice(0, 200) + "…"))))];
}
pages.agents = async root => {
  const out = h("div", { class: "page" });
  const draw = () => fill(out, head(t("nav_agents"), t("agents_sub")),
    scenarioControls(async () => { S.lastRun = await api("/api/run", { as_of: $("asof").value, unit: S.scn.unit, multiplier: S.scn.mult, language: S.lang }); toast(t("decision_created")); draw(); }, t("run_agents")),
    ...(S.lastRun ? [h("div", { class: "range" }, h("span", {}, t("status") + ":"), pill(t("st_" + S.lastRun.status), S.lastRun.status === "PENDING" ? "high" : ""), h("span", { class: "spacer" }), h("button", { class: "btn", onclick: () => go("decisions") }, t("open_decisions"))), cardView(S.lastRun), ...proposalsView(S.lastRun)]
      : [empty(t("run_prompt"), "agents")]));
  draw(); fill(root, out);
};

// ───────────────────────── digital twin simulator ─────────────────────────
pages.simulator = async root => {
  const out = h("div", { class: "page" });
  const draw = () => {
    const r = S.sim;
    fill(out, head(t("nav_simulator"), t("sim_sub")),
      scenarioControls(async () => { S.sim = await api("/api/simulate", { as_of: $("asof").value, unit: S.scn.unit, multiplier: S.scn.mult, language: S.lang }); draw(); }, t("run_preview"),
        h("button", { class: "btn", disabled: !can("run") || !r, title: t("save_tip"), onclick: async () => { try { S.lastRun = await api("/api/run", { as_of: $("asof").value, unit: S.scn.unit, multiplier: S.scn.mult, language: S.lang }); toast(t("decision_created")); go("decisions"); } catch (e) { toast(e.message, "bad"); } } }, t("save_decision"))),
      r ? simResult(r) : empty(t("sim_prompt"), "simulator"));
  };
  draw(); fill(root, out);
};
function simResult(r) {
  const U = ["ER", "ICU", "WARD"], b = r.before, a = r.after, acts = r.decision.accepted.flatMap(p => p.actions);
  const trow = (label, k, f = x => NUM(num(x))) => [label, ...U.map(u => h("span", {}, f(b[u][k]), " → ", f(a[u][k])))];
  const pct = x => NUM(x == null ? "—" : num(x) + "%"), whole = x => NUM(x == null ? "—" : num(x, 0));
  return h("div", { class: "grid", style: "gap:16px" },
    h("div", { class: "callout" }, icon("info"), t("sim_scenario", { u: unitName(r.scenario.unit), m: r.scenario.demand_multiplier, when: when(r.as_of) })),
    h("div", { class: "grid g2" },
      panel({ title: t("baseline"), prov: "observed", sub: t("baseline_sub"), source: ["beds", "bed_stays", "daily_metrics"] }, table([{ label: t("department") }, { label: t("beds_total"), n: 1 }, { label: t("peak_demand_beds"), n: 1 }, { label: t("peak_util"), n: 1 }, { label: t("shortfall"), n: 1 }], U.map(u => [unitName(u), whole(b[u].beds), NUM(num(b[u].peak_demand_beds)), pct(b[u].peak_utilisation_pct), whole(b[u].shortfall_beds)]))),
      panel({ title: t("proposed_actions"), prov: "agent", sub: t("proposed_actions_sub") }, acts.length ? h("ul", { class: "plain small" }, acts.map(x => h("li", {}, actionText(x)))) : empty(t("no_actions"), "check"))),
    h("div", { class: "grid g2" },
      panel({ title: t("before_after"), prov: "simulation", unit: t("u_beds"), sub: t("capacity_arith"), note: t("sim_note") },
        chart({ labels: U.map(unitName), unit: t("u_beds"), series: [{ name: t("beds_before"), values: U.map(u => b[u].beds), color: Charts.COLOR.observed }, { name: t("beds_after"), values: U.map(u => a[u].beds), color: Charts.COLOR.agent }, { name: t("peak_demand"), values: U.map(u => b[u].peak_demand_beds), color: Charts.COLOR.forecast }] }, "bars")),
      panel({ title: t("impact"), prov: "simulation", sub: t("impact_sub") },
        table([{ label: t("metric") }, ...U.map(u => ({ label: unitName(u), n: 1 }))], [trow(t("beds_total"), "beds", whole), trow(t("shortfall"), "shortfall_beds", whole), trow(t("peak_util"), "peak_utilisation_pct", pct)]),
        h("div", { class: "grid g3" }, stat(t("beds_moved"), num(Math.max(...Object.values(r.bed_change)), 0), t("u_beds")), stat(t("rescheduled"), r.rescheduled_bookings, t("u_bookings")),
          stat(t("cost_change"), r.capacity_cost_change_egp_per_day == null ? "—" : num(r.capacity_cost_change_egp_per_day, 0), "EGP/" + t("u_day"))), h("p", { class: "small faint" }, r.cost_note))),
    policyHint(r.decision),
    h("div", { class: "grid g3" }, r.unavailable.map(u => unavailable(t("na_" + u.metric.split(" ")[0].toLowerCase()), u.reason))),
    r.decision.rejected.length > 0 && panel({ title: t("constraints"), sub: t("rejected_sub") }, h("ul", { class: "plain small" }, r.decision.rejected.map(x => h("li", {}, h("b", {}, t("agent_" + x.proposal.agent)), ": ", x.reasons.join(" · "))))),
    panel({ title: t("pack_ref"), prov: "observed", sub: t("pack_ref_sub") }, table([{ label: t("scenario") }, { label: `${t("wait_avg")} (${t("u_min")})`, n: 1 }, { label: `${t("wait_p90")} (${t("u_min")})`, n: 1 }], r.pack_reference.map(p => [p.name, NUM(num(p.results.mean_wait_minutes)), NUM(num(p.results.p90_wait_minutes))]))));
}

// ───────────────────────── decisions ─────────────────────────
const STATUS_COLS = [["PENDING", ["PENDING"]], ["APPLIED", ["APPLIED"]], ["REJECTED", ["REJECTED"]], ["ROLLED_BACK", ["ROLLED_BACK", "SUPERSEDED"]]];
pages.decisions = async root => {
  const [list, sb] = await Promise.all([api("/api/decisions"), api("/api/sandbox")]);
  const card = d => h("button", { class: "dcard", onclick: () => openDecision(d.id) }, h("div", { class: "range" }, pill(t("st_" + d.status), d.stale ? "warn" : ""), d.stale && pill(t("stale"), "warn"), h("span", { class: "spacer" }), h("span", { class: "small faint" }, when(d.created_at))),
    h("b", {}, `${unitName(d.scenario.unit)} ×${d.scenario.demand_multiplier}`), h("div", { class: "small mute" }, d.bottleneck ? poolName(d.bottleneck) : t("none_found")),
    h("div", { class: "small" }, t("counts", { a: d.accepted, r: d.rejected })), h("div", { class: "small faint" }, d.created_by + (d.decided_by ? ` → ${d.decided_by}` : "")));
  fill(root, h("div", { class: "page" }, head(t("nav_decisions"), t("decisions_sub")), h("div", { class: "callout" }, icon("info"), t("approve_note")),
    panel({ title: t("sandbox"), prov: "simulation", sub: t("sandbox_sub"), right: h("button", { class: "btn danger small", disabled: !can("decide") || !sb.applied.length, title: can("decide") ? "" : t("no_permission"), onclick: async () => { try { await api("/api/rollback", {}); toast(t("rolled_back")); render(); } catch (e) { toast(e.message, "bad"); } } }, t("rollback_last")) },
      sb.applied.length ? h("ul", { class: "plain small" }, sb.applied.map(x => h("li", {}, h("b", {}, x.decision_id), ": ", x.actions.map(actionText).join(" · ")))) : empty(t("sandbox_empty"), "check")),
    list.length ? h("div", { class: "cols" }, STATUS_COLS.map(([title, set]) => h("section", { class: "grid", style: "gap:10px" }, h("h3", {}, t("st_" + title), " ", h("span", { class: "faint" }, NUM(list.filter(d => set.includes(d.status)).length))),
      ...list.filter(d => set.includes(d.status)).map(card)))) : empty(t("no_decisions"), "decisions")));
};
async function openDecision(id) {
  const dr = $("drawer");
  try {
    const [d, audit] = await Promise.all([api("/api/decisions/" + id), api("/api/audit")]);
    const close = () => { dr.hidden = true; dr.replaceChildren(); }, note = h("input", { placeholder: t("note_ph"), "aria-label": t("note_ph") });
    const act = kind => async () => { try { await api(`/api/decisions/${id}/${kind}`, { note: note.value, reason: note.value }); toast(t(kind === "approve" ? "applied_ok" : "rejected_ok")); close(); render(); } catch (e) { toast(e.message, "bad"); } };
    const noActions = !d.accepted.some(p => p.actions.some(a => a.pool || a.booking_id));
    fill(dr, h("div", {}, h("div", { class: "range" }, h("h2", {}, d.id), h("span", { class: "spacer" }), h("button", { class: "btn icon", onclick: close, "aria-label": t("close") }, "×")),
      h("div", { class: "range" }, pill(t("st_" + d.status), d.stale ? "warn" : ""), d.stale && pill(t("stale"), "warn"), h("span", { class: "small mute" }, `${unitName(d.scenario.unit)} ×${d.scenario.demand_multiplier} · ${when(d.as_of)}`)),
      d.stale && h("div", { class: "callout warn" }, icon("warn"), `${t("stale_msg")} ${d.stale_reason}`),
      d.status === "PENDING" && h("div", { class: "panel" }, noActions && h("div", { class: "callout" }, icon("info"), t("no_effect")), h("div", { class: "range", style: "flex-wrap:wrap" }, note,
        h("button", { class: "btn primary", disabled: !can("decide") || d.stale || noActions, title: can("decide") ? "" : t("no_permission"), onclick: act("approve") }, t("approve")),
        h("button", { class: "btn danger", disabled: !can("decide"), onclick: act("reject") }, noActions ? t("dismiss") : t("reject"))), !can("decide") && h("p", { class: "small mute" }, t("no_permission"))),
      d.decided_by && h("p", { class: "small mute" }, t("decided", { who: d.decided_by, when: when(d.decided_at) }), d.note ? ` — ${d.note}` : ""),
      cardView(d), ...proposalsView(d), panel({ title: t("audit_trail") }, h("ul", { class: "plain small" }, audit.filter(e => e.detail && e.detail.decision === id).map(e => h("li", {}, `${when(e.time)} · ${e.actor} · ${e.action}`))))));
    dr.hidden = false; dr.onclick = e => { if (e.target === dr) close(); };
  } catch (e) { toast(e.message, "bad"); }
}

// ───────────────────────── assistant ─────────────────────────
function citeText(text, onCite) {
  const parts = [];
  text.split(/(\[E\d+(?:,\s*E\d+)*\])/).forEach(p => {
    const m = p.match(/^\[(E\d+(?:,\s*E\d+)*)\]$/);
    if (m) m[1].split(/,\s*/).forEach(id => parts.push(h("button", { class: "cite", type: "button", onclick: () => onCite(id), title: t("source") }, id.slice(1))));
    else if (p) parts.push(p);
  });
  return parts;
}
pages.assistant = async root => {
  const hist = await api("/api/assistant");
  if (!S.chat.length && hist.history.length) S.chat = hist.history.map(x => ({ role: x.role, text: x.text }));
  const list = h("div", { class: "msgs", "aria-live": "polite" });
  const input = h("textarea", { rows: 1, placeholder: t("ask_ph"), "aria-label": t("ask_ph") });
  const sendBtn = h("button", { class: "btn primary", type: "button" }, icon("send"), t("send"));
  const modeChip = m => pill({ local: t("mode_local"), llm: t("mode_llm"), llm_fallback: t("mode_fallback") }[m] || m, m === "llm" ? "good" : m === "llm_fallback" ? "high" : "info");
  const draw = () => {
    fill(list, S.chat.length ? S.chat.map((m, i) => {
      const box = h("div", { class: "msg " + m.role });
      const det = m.evidence && m.evidence.length ? h("details", { id: `ev-${i}` }, h("summary", { class: "small mute" }, t("sources_n", { n: m.evidence.length })),
        h("ul", { class: "plain small" }, m.evidence.map(e => h("li", { id: `ev-${i}-${e.id}` }, h("b", {}, `${e.id.slice(1)}. ${e.label}`), ": ", e.text, h("div", { class: "faint" }, `${t("source")}: ${e.table}${e.period ? " · " + e.period : ""}`))))) : null;
      fill(box, h("div", { class: "bubble", dir: "auto" }, m.role === "assistant" ? citeText(m.text, id => { const d = $(`ev-${i}`); if (d) { d.open = true; $(`ev-${i}-${id}`)?.scrollIntoView({ block: "nearest" }); } }) : m.text),
        m.role === "assistant" && m.mode && h("div", { class: "range small" }, modeChip(m.mode), m.notes && m.notes.length > 0 && h("span", { class: "faint" }, t("note_llm", { n: m.notes.join(", ") }))), det,
        m.suggestions && m.suggestions.length > 0 && h("div", { class: "chips" }, m.suggestions.map(s => h("button", { type: "button", onclick: () => ask(s) }, s))));
      return box;
    }) : [empty(t("chat_empty"), "assistant"), h("div", { class: "chips" }, t("suggestions").split("|").map(s => h("button", { type: "button", onclick: () => ask(s) }, s)))]);
    list.scrollTop = list.scrollHeight;
  };
  async function ask(text) {
    text = text.trim(); if (!text) return;
    S.chat.push({ role: "user", text }); input.value = ""; draw();
    const typing = h("div", { class: "msg" }, h("div", { class: "bubble typing" }, h("span"), h("span"), h("span"))); list.append(typing); list.scrollTop = list.scrollHeight; sendBtn.disabled = true;
    try { const r = await api("/api/assistant", { message: text, as_of: $("asof").value, language: S.lang }); S.chat.push({ role: "assistant", text: r.answer, evidence: r.evidence, mode: r.mode, notes: r.notes, suggestions: r.suggestions }); }
    catch (e) { S.chat.push({ role: "assistant", text: `${t("error")}: ${e.message}`, mode: "local" }); }
    finally { sendBtn.disabled = false; draw(); input.focus(); }
  }
  sendBtn.addEventListener("click", () => ask(input.value));
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); } });
  fill(root, h("div", { class: "page" }, head(t("nav_assistant"), t("assistant_sub")),
    h("div", { class: "callout" + (hist.mode === "llm" ? " good" : "") }, icon("info"), hist.mode === "llm" ? t("assistant_llm_on") : t("assistant_local")),
    panel({ title: t("conversation"), prov: "agent", right: h("button", { class: "btn small ghost", onclick: async () => { await api("/api/assistant/reset", {}); S.chat = []; draw(); } }, icon("trash"), t("clear_chat")) },
      h("div", { class: "chat" }, list, h("div", { class: "composer" }, input, sendBtn)), h("p", { class: "small faint" }, t("assistant_limits")))));
  draw();
};

// ───────────────────────── sources & policies ─────────────────────────
pages.sources = async root => {
  const query = $("q").value.trim(), k = await api("/api/knowledge?q=" + encodeURIComponent(query));
  fill(root, h("div", { class: "page" }, head(t("nav_sources"), t("sources_sub")), h("div", { class: "callout" }, icon("info"), t("retrieval_only")),
    query ? panel({ title: t("results_for", { q: query }), prov: "observed", sub: t("n_items", { n: k.passages.length }) }, k.passages.length ? k.passages.map(p => h("div", { class: "agent-tile" },
      h("div", { class: "range" }, h("b", {}, p.source), h("span", { class: "spacer" }), pill(t("kind_" + p.kind.replace(" ", "_")), "info"), h("span", { class: "chip" }, `${t("score")} ${num(p.score, 2)}`)), h("p", { class: "mute", dir: "auto" }, p.text))) : empty(t("no_results"), "search")) : empty(t("search_prompt"), "search"),
    h("div", { class: "grid g2" },
      panel({ title: t("corpus_sources"), prov: "observed", source: "combined.json", sub: k.corpus ? t("corpus_stats", { n: k.corpus.loaded, d: Object.values(k.corpus.dropped).reduce((a, b) => a + b, 0) }) : "" },
        table([{ label: t("title") }, { label: t("country") }, { label: t("version") }, { label: t("status") }], k.sources.map(s => [s.title, s.country || "—", s.version || "—", s.status || "—"]))),
      panel({ title: t("policy_values"), prov: "agent", sub: t("policy_values_sub") }, k.policy_values.length ? table([{ label: t("department") }, { label: t("value"), n: 1 }, { label: t("source") }], k.policy_values.map(v => [unitName(v.unit), NUM(v.value), h("span", { class: "small mute" }, v.source)])) : unavailable(t("no_policy_values"), t("no_policy_values_d")))),
    panel({ title: t("pack_policies"), prov: "observed", sub: t("pack_policies_sub") }, h("ul", { class: "plain small" }, k.pack_policies.map(p => h("li", {}, h("b", {}, p.source), ": ", p.text))))));
};

// ───────────────────────── data quality & system ─────────────────────────
pages.system = async root => {
  const [st, ql, pol] = await Promise.all([api("/api/status"), api("/api/quality"), api("/api/policies")]);
  const unitForm = u => { const cur = pol.nurse_ratio.find(p => p.unit === u), input = h("input", { type: "number", min: 0.5, step: 0.5, value: cur ? cur.value : "", placeholder: t("max_patients"), "aria-label": `${unitName(u)} ${t("max_patients")}`, disabled: !can("policy") });
    const send = v => async () => { try { await api("/api/policies", { unit: u, value: v }); toast(t("policy_saved")); render(); } catch (e) { toast(e.message, "bad"); } };
    return [h("b", {}, unitName(u)), h("span", { class: "small" }, cur ? [`${t("current")}: `, NUM(cur.value), h("div", { class: "faint" }, cur.source)] : h("span", { class: "mute" }, t("not_set"))),
      h("div", { class: "range" }, input, h("button", { class: "btn small primary", disabled: !can("policy"), onclick: () => send(input.value)() }, t("save")), cur && h("button", { class: "btn small danger", disabled: !can("policy"), onclick: send(null) }, t("remove")))]; };
  const result = h("div"), testBtn = h("button", { class: "btn", disabled: !can("run"), title: can("run") ? "" : t("no_permission"), onclick: async () => {
    testBtn.disabled = true; fill(result, h("span", { class: "mute" }, t("testing")));
    try { const r = await api("/api/llm-test", {}); fill(result, pill(r.ok ? t("test_ok") : t("test_fail"), r.ok ? "good" : "high"), " ", h("span", {}, t("llm_cat_" + r.category)), r.latency_ms != null && h("span", { class: "faint small" }, ` · ${r.latency_ms} ms`), r.models_available != null && h("span", { class: "faint small" }, ` · ${t("models_n", { n: r.models_available })}`)); S.llmOn = r.ok || S.llmOn; if (r.ok) buildChrome(); }
    catch (e) { fill(result, pill(t("test_fail"), "high"), " ", e.message); } finally { testBtn.disabled = !can("run"); } } }, icon("refresh"), t("test_connection"));
  const L = st.llm, ls = L.stats;
  fill(root, h("div", { class: "page" }, head(t("ttl_system"), t("system_sub")),
    h("div", { class: "grid g2" },
      panel({ title: t("llm"), sub: `${L.provider} · ${L.model}`, right: pill(L.configured ? t("configured") : t("not_configured"), L.configured ? "good" : "high") },
        h("p", { class: "mute" }, L.configured ? t("llm_on") : t("llm_off")), h("div", { class: "range", style: "flex-wrap:wrap" }, testBtn, result),
        h("div", { class: "grid g4" }, stat(t("llm_calls"), ls.calls), stat(t("llm_retries"), ls.retries), stat(t("llm_failures"), ls.failures), stat(t("llm_cache"), ls.cache_hits)),
        h("p", { class: "small faint" }, ls.last_error ? t("llm_last_error", { c: t("llm_cat_" + ls.last_error) }) : t("llm_no_errors")),
        h("div", { class: "callout" }, icon("info"), h("span", {}, t("env_how")))),
      panel({ title: t("persistence"), sub: t("persistence_sub") }, h("div", { class: "grid g4" }, stat(t("decisions_stored"), st.persistence.decisions), stat(t("in_sandbox"), st.persistence.applied_in_sandbox), stat(t("sandbox_rev"), st.persistence.sandbox_revision), stat(t("audit_events"), st.persistence.audit_events)),
        st.persistence.repairs.length > 0 && h("div", { class: "callout warn" }, icon("warn"), t("repairs", { n: st.persistence.repairs.length })))),
    panel({ title: t("features"), sub: t("features_sub") }, table([{ label: t("feature") }, { label: t("status") }, { label: t("note") }], st.features.map(f => [f.name, pill(f.available ? t("available") : t("unavailable"), f.available ? "good" : "high"), h("span", { class: "small mute" }, f.note)]))),
    panel({ title: t("data_quality"), prov: "observed", source: "data/*.csv", period: ql.period, sub: t("quality_sub", { p: ql.summary.pass || 0, w: ql.summary.warn || 0, f: ql.summary.fail || 0 }) },
      table([{ label: t("check") }, { label: t("status") }, { label: t("value"), n: 1 }, { label: t("detail") }], ql.checks.map(c => [c.name, pill(t("q_" + c.status), c.status === "pass" ? "good" : c.status === "warn" ? "high" : "critical"), NUM(c.value), h("span", { class: "small mute" }, c.detail)]))),
    panel({ title: t("policy_entry"), prov: "agent", sub: t("policy_entry_sub") }, can("policy") ? null : h("div", { class: "callout" }, icon("info"), t("manager_only")), table([{ label: t("department") }, { label: t("current") }, { label: "" }], ["ER", "ICU", "WARD"].map(unitForm))),
    panel({ title: t("tables"), source: "data/", sub: t("tables_sub", { d: ql.days }) }, h("div", { class: "grid g4" }, Object.entries(ql.tables).map(([k, v]) => h("div", { class: "small" }, h("span", { class: "mute" }, k), ": ", NUM(num(v, 0))))))));
};

// ───────────────────────── shell: auth, nav, routing ─────────────────────────
const NAV = [["command", ["overview", "alerts"]], ["hospital", ["beds", "er", "staff", "or"]], ["intelligence", ["forecast", "agents", "simulator", "decisions", "assistant", "sources"]], ["system", ["system"]]];
function buildNav() {
  $("nav").replaceChildren(...NAV.flatMap(([g, items]) => [h("div", { class: "nav-group" }, t("grp_" + g)), ...items.map(p => h("a", { class: "nav-item", href: "#/" + p, "data-page": p, "aria-current": S.page === p ? "page" : null },
    icon(p), h("span", {}, t("nav_" + p)), p === "alerts" && h("span", { class: "nav-badge", "data-badge": "1", hidden: !S.alertCount }, S.alertCount || "")))]));
}
function buildUser() {
  if (!S.user) return;
  $("user").replaceChildren(h("div", { class: "avatar" }, S.user.username[0].toUpperCase()), h("div", { class: "who" }, h("b", {}, S.user.username), h("span", {}, t("role_" + S.user.role))),
    h("button", { class: "btn icon ghost bell", onclick: () => go("alerts"), "aria-label": t("nav_alerts") }, icon("alerts"), S.alertCount ? h("span", { class: "badge" }, S.alertCount) : null),
    h("button", { class: "btn icon ghost", onclick: signOut, "aria-label": t("sign_out"), title: t("sign_out") }, icon("logout")));
}
function buildStatus() {
  const ok = S.health && S.health.ok;
  $("status-pill").className = "status-pill" + (ok ? "" : " bad");
  $("status-pill").replaceChildren(h("span", { class: `dot ${ok ? "on" : "bad"}` }), h("span", { class: "t" }, ok ? t("system_running") : t("system_offline")));
}
function buildChrome() {
  document.documentElement.lang = S.lang; document.documentElement.dir = S.lang === "ar" ? "rtl" : "ltr";
  document.querySelectorAll("[data-t]").forEach(e => e.textContent = t(e.dataset.t));
  document.querySelectorAll("[data-tp]").forEach(e => e.setAttribute("placeholder", t(e.dataset.tp)));
  $("lang").textContent = S.lang === "ar" ? "English" : "العربية"; $("login-lang").textContent = $("lang").textContent;
  $("menu").replaceChildren(icon("menu")); $("search-ico").replaceChildren(icon("search")); $("cal-ico").replaceChildren(icon("calendar"));
  buildNav(); buildUser(); buildStatus();
  if (S.meta) $("side-foot").replaceChildren(h("h4", {}, t("data_status")), h("div", { class: "row" }, h("span", { class: `dot ${S.health && S.health.ok ? "on" : "bad"}` }), t("api_ok")),
    h("div", { class: "row" }, h("span", { class: "dot on" }), t("synthetic")), h("div", { class: "row" }, icon("calendar"), t("data_period", { a: day(S.meta.first_day), b: day(S.meta.last_day) })),
    h("div", { class: "row" }, h("span", { class: `dot ${S.llmOn ? "on" : "off"}` }), S.llmOn ? t("llm_on_short") : t("llm_off_short")));
}
function go(page) { location.hash = "#/" + page; }
async function render() {
  const page = (location.hash.replace("#/", "") || "overview"); S.page = pages[page] ? page : "overview";
  buildNav(); document.body.classList.remove("nav-open");
  const root = $("view"); fill(root, skeleton(S.page === "overview" ? "kpi,tall" : "tall"));
  try { await pages[S.page](root); } catch (e) { fill(root, h("div", { class: "page" }, errorBox(e.message, render))); }
}
function signOut() {
  if (S.token) fetch("/api/logout", { method: "POST", headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
  S.token = null; S.user = null; S.chat = []; sessionStorage.removeItem("token"); $("app").hidden = true; $("login").hidden = false; buildChrome();
}
async function refreshHealth() { try { S.health = await api("/api/health"); } catch { S.health = { ok: false }; } buildStatus(); }
async function start() {
  try { S.user = await api("/api/me"); S.meta = await api("/api/meta"); S.health = await api("/api/health"); S.llmOn = S.health.llm.configured; }
  catch { $("login").hidden = false; $("app").hidden = true; buildChrome(); return; }
  $("login").hidden = true; $("app").hidden = false;
  const input = $("asof"); input.min = S.meta.first_day + "T00:00"; input.max = S.meta.last_day + "T23:00"; if (!input.value) input.value = S.meta.default_as_of.slice(0, 16);
  buildChrome(); render(); setInterval(refreshHealth, 60000);
}
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); $("login-error").textContent = "";
  try { const r = await api("/api/login", { username: $("u").value, password: $("p").value }); S.token = r.token; sessionStorage.setItem("token", r.token); $("p").value = ""; start(); }
  catch (err) { $("login-error").textContent = err.message; }
});
const flip = () => { S.lang = S.lang === "ar" ? "en" : "ar"; localStorage.setItem("lang", S.lang); buildChrome(); if (S.user) render(); };
$("lang").addEventListener("click", flip); $("login-lang").addEventListener("click", flip);
$("asof").addEventListener("change", () => { S.lastRun = null; S.sim = null; render(); });
$("search").addEventListener("submit", e => { e.preventDefault(); location.hash === "#/sources" ? render() : go("sources"); });
$("menu").addEventListener("click", () => document.body.classList.toggle("nav-open")); $("scrim").addEventListener("click", () => document.body.classList.remove("nav-open"));
window.addEventListener("hashchange", () => S.user && render());
buildChrome(); start();
