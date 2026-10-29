// Runs the real dashboard scripts against a live server in a minimal fake DOM and exercises every page.
// Usage: node dom_smoke.js <port> <token> ; prints one JSON report line. (Driven by tests/test_frontend.py)
const fs = require("fs"), path = require("path");
const root = path.resolve(__dirname, "..");
const [port, token] = process.argv.slice(2);

class FNode {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.parent = null; this.listeners = {}; this.style = { setProperty() {} }; this._text = ""; this.value = ""; this.clientWidth = 700; this.offsetWidth = 150; this.offsetHeight = 60; this.isConnected = true; }
  get nodeType() { return this.tag === "#text" ? 3 : 1; }
  get className() { return this.attrs.class || ""; } set className(v) { this.attrs.class = v; }
  get classList() { const n = this; return { add: c => { n.attrs.class = [...new Set((n.className + " " + c).split(" ").filter(Boolean))].join(" "); }, remove: c => { n.attrs.class = n.className.split(" ").filter(x => x && x !== c).join(" "); }, toggle: c => n.classList.add(c), contains: c => n.className.split(" ").includes(c) }; }
  get dataset() { const o = {}; for (const [k, v] of Object.entries(this.attrs)) if (k.startsWith("data-")) o[k.slice(5)] = v; return o; }
  get hidden() { return "hidden" in this.attrs; } set hidden(v) { v ? this.attrs.hidden = "" : delete this.attrs.hidden; }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k] ?? null; } removeAttribute(k) { delete this.attrs[k]; }
  append(...kids) { for (const k of kids) { const n = typeof k === "string" ? Object.assign(new FNode("#text"), { _text: k }) : k; n.parent = this; this.children.push(n); } }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  addEventListener(e, f) { (this.listeners[e] ??= []).push(f); }
  set textContent(v) { this.children = []; this._text = String(v); } get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map(c => c.textContent).join(" "); }
  walk(f) { f(this); this.children.forEach(c => c.walk(f)); }
  find(pred) { const out = []; this.walk(n => pred(n) && out.push(n)); return out; }
  matches(sel) { const m = sel.match(/^\[([\w-]+)\]$/); return !!m && m[1] in this.attrs; }
  querySelector(sel) { return this.find(n => n.matches(sel))[0] || null; } querySelectorAll(sel) { return this.find(n => n.matches(sel)); }
  focus() {} scrollIntoView() {}
  click() { for (const f of this.listeners.click || []) f({ target: this, preventDefault() {} }); }
}
const ids = {};
for (const m of fs.readFileSync(path.join(root, "dashboard.html"), "utf8").matchAll(/id="([\w-]+)"/g)) ids[m[1]] = Object.assign(new FNode("div"), { attrs: { id: m[1] } });
const doc = new FNode("#document"); doc.documentElement = new FNode("html"); doc.body = new FNode("body");
Object.assign(doc, { getElementById: id => ids[id], createElement: t => new FNode(t), createElementNS: (n, t) => new FNode(t), createTextNode: s => Object.assign(new FNode("#text"), { _text: s }),
  querySelectorAll: sel => Object.values(ids).flatMap(n => n.querySelectorAll(sel)).concat(sel === "[data-t]" || sel === "[data-tp]" ? [] : []) });
// elements with data-t / data-tp in the static HTML
const html = fs.readFileSync(path.join(root, "dashboard.html"), "utf8");
const statics = [];
for (const m of html.matchAll(/<(\w+)[^>]*\bdata-(t|tp)="(\w+)"[^>]*>/g)) { const n = new FNode(m[1]); n.attrs["data-" + m[2]] = m[3]; statics.push(n); }
doc.querySelectorAll = sel => statics.filter(n => n.matches(sel));
const store = () => { const s = {}; return { getItem: k => s[k] ?? null, setItem: (k, v) => { s[k] = String(v); }, removeItem: k => { delete s[k]; } }; };
const handlers = {};
Object.assign(globalThis, { document: doc, localStorage: store(), sessionStorage: store(), ResizeObserver: class { observe() {} },
  location: { hash: "" }, window: { addEventListener: (e, f) => (handlers[e] ??= []).push(f), innerWidth: 1200 }, history: {} });
globalThis.addEventListener = window.addEventListener;
const realFetch = fetch; globalThis.fetch = (p, o) => realFetch(`http://127.0.0.1:${port}${p}`, o);
sessionStorage.setItem("token", token); localStorage.setItem("lang", "en");

const code = ["i18n.js", "charts.js", "app.js"].map(f => fs.readFileSync(path.join(root, "static", f), "utf8")).join("\n");
(0, eval)(code + "\n;globalThis.__t = { S, render, go, pages, can, I18N };");
const sleep = ms => new Promise(r => setTimeout(r, ms));
const view = () => ids.view;
async function settle() { let last = -1, stable = 0; for (let i = 0; i < 120; i++) { await sleep(60); const len = view().textContent.length, ready = !view().find(n => n.className.startsWith("skeleton")).length && view().children.length; stable = ready && len === last ? stable + 1 : 0; last = len; if (stable >= 2) return; } }
const text = () => view().textContent;
const btn = label => ids.view.find(n => n.tag === "button" && n.textContent.trim() === label)[0] || doc.getElementById("drawer").find(n => n.tag === "button" && n.textContent.trim() === label)[0];
const report = { pages: {}, errors: [] };
(async () => {
  await settle(); await sleep(300);
  const I = __t.I18N;
  for (const lang of ["en", "ar"]) {
    __t.S.lang = lang; localStorage.setItem("lang", lang); document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
    for (const p of Object.keys(__t.pages)) {
      location.hash = "#/" + p; (handlers.hashchange || []).forEach(f => f()); await settle();
      const svgs = view().find(n => n.tag === "svg").length, callout = view().find(n => n.className.includes("callout warn") && /Something went wrong|حدث خطأ/.test(n.textContent)).length;
      report.pages[`${lang}:${p}`] = { chars: text().length, svgs, error: callout > 0 || view().find(n => n.className === "errorbox").length > 0, sample: text().length < 80 ? text() : undefined, junk: (text().match(/\bundefined\b|\bNaN\b|\[object Object\]/g) || []).length };
      if (callout) report.errors.push(`${lang}:${p}: ${text().slice(0, 160)}`);
    }
  }
  ids.lang.click(); await sleep(300); report.langToggle = document.documentElement.dir;     // ar -> en flips the page direction
  __t.S.lang = "en"; localStorage.setItem("lang", "en");
  // agents → run
  location.hash = "#/agents"; (handlers.hashchange || []).forEach(f => f()); await settle();
  if (!btn(I.en.run_agents)) { console.log(JSON.stringify({ crash: "no run button; view=" + text().slice(0, 400) + " | lang=" + __t.S.lang })); process.exit(1); }
  btn(I.en.run_agents).click(); await sleep(1800);
  report.agentsCard = /Executive card/.test(text()) && /Accepted proposals/.test(text());
  // simulator → preview (baseline, actions, before/after, unavailable metrics)
  location.hash = "#/simulator"; (handlers.hashchange || []).forEach(f => f()); await settle();
  btn(I.en.run_preview).click(); await sleep(1800);
  report.simulator = /Beds before and after/.test(text()) && /Baseline/.test(text()) && /Waiting time change/.test(text()) && /No simulation engine/.test(text());
  // decisions → list, drawer, approve
  location.hash = "#/decisions"; (handlers.hashchange || []).forEach(f => f()); await settle();
  const card = view().find(n => n.className === "dcard")[0]; report.decisionCards = view().find(n => n.className === "dcard").length;
  if (card) { card.click(); await sleep(900); const dr = doc.getElementById("drawer"); report.drawerOpen = !dr.hidden && /Audit trail/.test(dr.textContent); const ap = btn(I.en.approve); report.approveEnabled = !!ap && !("disabled" in ap.attrs); if (ap && !("disabled" in ap.attrs)) { ap.click(); await sleep(1500); } }
  await settle(); report.appliedAfterApprove = /Applied/.test(text());
  // overview: digital twin zones and filter chips
  location.hash = "#/overview"; (handlers.hashchange || []).forEach(f => f()); await settle();
  report.twinZones = view().find(n => n.attrs["data-type"]).length; report.twinBeds = view().find(n => /^bed( |$)/.test(n.className)).length;
  report.kpiCards = view().find(n => n.className.startsWith("kpi ")).length;
  const chip = view().find(n => n.tag === "button" && n.textContent.trim() === I.en.grp2_critical)[0]; if (chip) chip.click(); report.twinFilter = !!chip;
  // assistant: ask a suggested question, get an answer with sources
  location.hash = "#/assistant"; (handlers.hashchange || []).forEach(f => f()); await settle();
  const sugg = view().find(n => n.tag === "button" && n.textContent.trim() === "How is ICU occupancy?")[0]; if (sugg) { sugg.click(); await sleep(1500); }
  report.assistant = /ICU: \d+ of 12 beds/.test(text()) && /Sources \(\d+\)/.test(text()) && /Local rules/.test(text());
  // system: connection test without a key
  location.hash = "#/system"; (handlers.hashchange || []).forEach(f => f()); await settle();
  const tb = btn(I.en.test_connection); if (tb) { tb.click(); await sleep(900); }
  report.llmTest = /Not connected/.test(text()) && /No API key is set/.test(text());
  report.policyInputs = view().find(n => n.tag === "input" && n.attrs.type === "number").length;
  // search → sources
  ids.q.value = "escalation"; ids.search.listeners.submit[0]({ preventDefault() {} }); await sleep(100); (handlers.hashchange || []).forEach(f => f()); await settle();
  report.sourcesResults = /Results for/.test(text());
  console.log(JSON.stringify(report));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify({ crash: String(e && e.stack || e) })); process.exit(1); });
