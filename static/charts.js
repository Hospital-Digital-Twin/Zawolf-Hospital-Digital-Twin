/* Small SVG chart kit (no dependencies). Every chart is responsive (redraws on resize), has a hover
   tooltip with units, and clickable legend toggles. Colours come from the provenance palette:
   observed = cyan, forecast = violet, simulation = amber, agent = green, alarm = coral. */
"use strict";
const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const COLOR = { observed: "var(--observed)", forecast: "var(--forecast)", sim: "var(--sim)", agent: "var(--agent)", alarm: "var(--alarm)" };
  const el = (tag, attrs = {}, ...kids) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); kids.forEach(k => e.append(k)); return e; };
  const html = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) k.startsWith("on") ? e.addEventListener(k.slice(2), v) : e.setAttribute(k, v); kids.forEach(k => e.append(k)); return e; };
  const tip = () => document.getElementById("tip");
  const uid = (() => { let n = 0; return () => "c" + ++n; })();

  function nice(lo, hi, ticks = 4) {
    if (hi === lo) hi = lo + 1;
    const raw = (hi - lo) / ticks, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
    const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
    const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, out = [];
    for (let v = a; v <= b + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }
  const fmtTick = v => Math.abs(v) >= 1000 ? (v / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "k" : v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  const text = (x, y, s, attrs = {}) => { const t = el("text", { x, y, ...attrs }); t.textContent = s; return t; };

  /** Draws into `host` and redraws on resize. `draw(width, hidden)` returns the SVG. */
  function mount(host, spec, draw) {
    host.classList.add("chart");
    const hidden = new Set();
    const wrap = html("div"), legend = html("div", { class: "legend", dir: document.documentElement.dir });
    host.replaceChildren(wrap, legend);
    const render = () => {
      const w = Math.max(240, host.clientWidth || 480);
      wrap.replaceChildren(draw(w, hidden));
    };
    spec.series.forEach(s => {
      if (s.noLegend) return;
      const b = html("button", { type: "button", "aria-pressed": "true", style: `color:${s.color}` }, html("i", { class: s.dashed ? "dash" : "" }), s.name);
      b.addEventListener("click", () => { hidden.has(s.name) ? hidden.delete(s.name) : hidden.add(s.name); b.setAttribute("aria-pressed", String(!hidden.has(s.name))); render(); });
      legend.append(b);
    });
    render();
    let last = host.clientWidth;
    new ResizeObserver(() => { if (host.isConnected && Math.abs(host.clientWidth - last) > 4) { last = host.clientWidth; render(); } }).observe(host);
  }

  function showTip(evt, title, rows) {
    const t = tip();
    t.replaceChildren(html("b", {}, title), ...rows.map(r => html("div", {}, html("span", {}, html("i", { style: `background:${r.color}` }), r.name), html("b", {}, r.text))));
    t.hidden = false;
    const w = t.offsetWidth, x = Math.min(window.innerWidth - w - 8, Math.max(8, evt.clientX + 14));
    t.style.left = x + "px"; t.style.top = Math.max(8, evt.clientY - t.offsetHeight - 12) + "px";
  }
  const hideTip = () => { tip().hidden = true; };

  /**
   * line({labels, series:[{name, values, color, dashed, band:[lo,hi], area, dots}], unit, fmt, height, yMin, yMax, marker:{index,label}})
   * null values leave gaps. `marker` draws a vertical line (e.g. the replay moment).
   */
  function line(host, spec) {
    const H = spec.height || 220, M = { l: 44, r: 12, t: 12, b: 28 };
    mount(host, spec, (W, hidden) => {
      const live = spec.series.filter(s => !hidden.has(s.name));
      const vals = live.flatMap(s => [...s.values, ...(s.band ? [...s.band[0], ...s.band[1]] : [])]).filter(v => v != null);
      const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.title || "" });
      if (!vals.length) { svg.append(text(W / 2, H / 2, spec.empty || "—", { "text-anchor": "middle" })); return svg; }
      const ticks = nice(spec.yMin ?? Math.min(0, ...vals), spec.yMax ?? Math.max(...vals));
      const lo = ticks[0], hi = ticks[ticks.length - 1], n = spec.labels.length;
      const x = i => M.l + (n < 2 ? (W - M.l - M.r) / 2 : i * (W - M.l - M.r) / (n - 1));
      const y = v => M.t + (H - M.t - M.b) * (1 - (v - lo) / (hi - lo));
      ticks.forEach(v => { svg.append(el("line", { class: "grid-line", x1: M.l, x2: W - M.r, y1: y(v), y2: y(v) }), text(M.l - 8, y(v) + 4, fmtTick(v), { "text-anchor": "end" })); });
      const every = Math.max(1, Math.ceil(n / Math.floor((W - M.l) / 70)));
      spec.labels.forEach((l, i) => { if (i % every === 0) svg.append(text(x(i), H - 8, (spec.tickFmt || String)(l), { "text-anchor": "middle" })); });
      if (spec.marker != null) svg.append(el("line", { x1: x(spec.marker.index), x2: x(spec.marker.index), y1: M.t, y2: H - M.b, stroke: "var(--faint)", "stroke-dasharray": "3 4" }), text(x(spec.marker.index) + 4, M.t + 10, spec.marker.label, { "text-anchor": "start" }));
      const gid = uid();
      live.forEach((s, si) => {
        if (s.band) {
          const idx = s.values.map((_, i) => i).filter(i => s.band[0][i] != null);
          if (idx.length) svg.append(el("polygon", { fill: s.color, opacity: .14, points: [...idx.map(i => `${x(i)},${y(s.band[1][i])}`), ...[...idx].reverse().map(i => `${x(i)},${y(s.band[0][i])}`)].join(" ") }));
        }
        let d = "", pen = false, firstY = null, lastX = null;
        s.values.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; if (!pen) firstY = firstY ?? i; pen = true; lastX = i; });
        if (s.area && d) {
          const defs = el("defs", {}, el("linearGradient", { id: `${gid}${si}`, x1: 0, y1: 0, x2: 0, y2: 1 }, el("stop", { offset: 0, "stop-color": s.color, "stop-opacity": .28 }), el("stop", { offset: 1, "stop-color": s.color, "stop-opacity": 0 })));
          svg.append(defs, el("path", { d: `${d}L${x(lastX)},${y(lo)}L${x(firstY)},${y(lo)}Z`, fill: `url(#${gid}${si})` }));
        }
        const path = el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2.2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": s.dashed ? "6 5" : "" });
        if (!s.dashed && !host.dataset.drawn) { path.setAttribute("class", "draw"); path.style.setProperty("--len", "2400"); }
        svg.append(path);
        if (s.dots) s.values.forEach((v, i) => v != null && svg.append(el("circle", { cx: x(i), cy: y(v), r: 3.4, fill: s.color, stroke: "var(--console)", "stroke-width": 1.5 })));
      });
      host.dataset.drawn = "1";
      const cross = el("line", { y1: M.t, y2: H - M.b, stroke: "var(--faint)", opacity: 0 }), hit = el("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" });
      svg.append(cross, hit);
      hit.addEventListener("pointermove", e => {
        const r = hit.getBoundingClientRect(), i = n < 2 ? 0 : Math.round((e.clientX - r.left) / r.width * (n - 1));
        cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 1);
        const rows = live.map(s => ({ name: s.name, color: s.color, text: s.values[i] == null ? "—" : (spec.fmt || fmtTick)(s.values[i]) + (spec.unit ? " " + spec.unit : "") }));
        const b = live.find(s => s.band && s.band[0][i] != null);
        if (b) rows.push({ name: spec.bandLabel || "range", color: b.color, text: `${fmtTick(b.band[0][i])}–${fmtTick(b.band[1][i])}` });
        showTip(e, (spec.tipFmt || spec.tickFmt || String)(spec.labels[i]), rows);
      });
      hit.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
      return svg;
    });
  }

  /** bars({labels, series:[{name, values, color}], unit, height, colors:(seriesIdx, i)=>colour|null}) grouped bars. */
  function bars(host, spec) {
    const H = spec.height || 200, M = { l: 44, r: 8, t: 12, b: 28 };
    mount(host, spec, (W, hidden) => {
      const live = spec.series.filter(s => !hidden.has(s.name));
      const vals = live.flatMap(s => s.values).filter(v => v != null);
      const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.title || "" });
      if (!vals.length) { svg.append(text(W / 2, H / 2, spec.empty || "—", { "text-anchor": "middle" })); return svg; }
      const ticks = nice(Math.min(0, ...vals), spec.yMax ?? Math.max(...vals)), lo = ticks[0], hi = ticks[ticks.length - 1];
      const n = spec.labels.length, band = (W - M.l - M.r) / n, bw = Math.max(2, Math.min(34, band * 0.72 / live.length));
      const y = v => M.t + (H - M.t - M.b) * (1 - (v - lo) / (hi - lo));
      ticks.forEach(v => svg.append(el("line", { class: "grid-line", x1: M.l, x2: W - M.r, y1: y(v), y2: y(v) }), text(M.l - 8, y(v) + 4, fmtTick(v), { "text-anchor": "end" })));
      const every = Math.max(1, Math.ceil(n / Math.floor((W - M.l) / 46)));
      spec.labels.forEach((l, i) => {
        const cx = M.l + band * (i + .5);
        if (i % every === 0) svg.append(text(cx, H - 8, (spec.tickFmt || String)(l), { "text-anchor": "middle" }));
        live.forEach((s, si) => {
          const v = s.values[i]; if (v == null) return;
          const bx = cx - (bw * live.length) / 2 + si * bw, y0 = y(Math.max(0, lo)), yy = y(v);
          svg.append(el("rect", { x: bx + 1, y: Math.min(y0, yy), width: Math.max(1, bw - 2), height: Math.max(1, Math.abs(y0 - yy)), rx: 3, fill: (spec.colors && spec.colors(spec.series.indexOf(s), i)) || s.color, opacity: .92 }));
        });
        const hit = el("rect", { x: M.l + band * i, y: M.t, width: band, height: H - M.t - M.b, fill: "transparent" });
        hit.addEventListener("pointermove", e => showTip(e, (spec.tipFmt || spec.tickFmt || String)(l), live.map(s => ({ name: s.name, color: s.color, text: s.values[i] == null ? "—" : (spec.fmt || fmtTick)(s.values[i]) + (spec.unit ? " " + spec.unit : "") }))));
        hit.addEventListener("pointerleave", hideTip);
        svg.append(hit);
      });
      return svg;
    });
  }

  /** ring(value, max, color, label) -> small donut gauge element. */
  function ring(value, max, color, label) {
    const R = 38, C = 2 * Math.PI * R, f = max ? Math.min(1, value / max) : 0;
    const svg = el("svg", { class: "ring", viewBox: "0 0 96 96", role: "img", "aria-label": label },
      el("circle", { cx: 48, cy: 48, r: R, fill: "none", stroke: "var(--raised)", "stroke-width": 9 }),
      el("circle", { cx: 48, cy: 48, r: R, fill: "none", stroke: color, "stroke-width": 9, "stroke-linecap": "round", "stroke-dasharray": `${C * f} ${C}`, transform: "rotate(-90 48 48)" }));
    svg.append(text(48, 53, label, { "text-anchor": "middle", style: "fill:var(--ink);font-size:16px;font-weight:650" }));
    return svg;
  }

  /** spark(values, color) -> tiny area line without axes. */
  function spark(values, color) {
    const W = 160, H = 36, v = values.filter(x => x != null);
    const svg = el("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    if (v.length < 2) return svg;
    let lo = Math.min(...v), hi = Math.max(...v); if (hi === lo) { hi += 1; lo -= 1; }
    const x = i => i * W / (values.length - 1), y = val => H - 3 - (H - 8) * (val - lo) / (hi - lo);
    let d = "", pen = false, first = null, last = null;
    values.forEach((val, i) => { if (val == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(val).toFixed(1)}`; first ??= i; last = i; pen = true; });
    const id = uid();
    svg.append(el("defs", {}, el("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 }, el("stop", { offset: 0, "stop-color": color, "stop-opacity": .3 }), el("stop", { offset: 1, "stop-color": color, "stop-opacity": 0 }))),
      el("path", { d: `${d}L${x(last)},${H}L${x(first)},${H}Z`, fill: `url(#${id})` }), el("path", { d, fill: "none", stroke: color, "stroke-width": 1.8, "vector-effect": "non-scaling-stroke" }));
    return svg;
  }

  return { COLOR, line, bars, ring, spark };
})();
