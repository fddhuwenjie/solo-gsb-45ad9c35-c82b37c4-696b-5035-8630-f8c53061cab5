/* 低空航线空域时窗审查台 — 原生 JS + SVG */
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("board");

const state = { route: { waypoints: [], speeds: [], departureTime: "", corridorHalfWidth: 0.3, unitScale: 100 }, nfzs: [] };
let analysis = null;
let mode = "select";
let selection = { type: null, id: null };
let view = { x: 0, y: 0, w: 1000, h: 620 };
let drawing = [];          // 绘制禁飞区中的顶点
let hover = null;          // 鼠标世界坐标（绘制预览）
let drag = null;           // 当前拖动
let analyzeTimer = null, saveTimer = null;
let panelDirty = true;

// ----------------------------------------------------------- 工具
const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") n.textContent = v; else n.setAttribute(k, v);
  }
  if (parent) parent.appendChild(n);
  return n;
};
const uid = (p) => p + Math.random().toString(36).slice(2, 8);
const num = (v, d = 0) => { const n = parseFloat(v); return Number.isFinite(n) ? n : d; };
const r2 = (v) => Math.round(v * 100) / 100;
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

function toWorld(clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  return {
    x: view.x + ((clientX - rect.left) / rect.width) * view.w,
    y: view.y + ((clientY - rect.top) / rect.height) * view.h,
  };
}
function screenScale() {
  const rect = svg.getBoundingClientRect();
  return view.w / rect.width; // 世界单位 / 像素
}

// ----------------------------------------------------------- API
async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  return res.json();
}
async function loadState() {
  const s = await api("/api/state");
  Object.assign(state, s);
  if (!state.route) state.route = { waypoints: [], speeds: [], departureTime: "", corridorHalfWidth: 0.3, unitScale: 100 };
  state.route.waypoints ||= [];
  state.route.speeds ||= [];
  state.route.unitScale ||= 100;
  state.route.corridorHalfWidth ||= 0.3;
  state.nfzs ||= [];
}
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    await api("/api/state", { method: "PUT", body: JSON.stringify(state) });
  }, 500);
}
function scheduleAnalyze() {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(runAnalyze, 350);
}
async function runAnalyze() {
  $("#analyzeStatus").textContent = "审查中…";
  try {
    analysis = await api("/api/analyze", { method: "POST", body: JSON.stringify(state) });
  } catch (e) {
    analysis = { ok: false, errors: [{ message: "分析请求失败：" + e }], entries: [] };
  }
  $("#analyzeStatus").textContent = "";
  renderBoard();
  renderResults();
  renderDynamicPanel();
}

// ----------------------------------------------------------- SVG 渲染
function starPath(cx, cy, rOuter, rInner) {
  const pts = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? rOuter : rInner;
    const a = -Math.PI / 2 + (i * Math.PI) / 5;
    pts.push(`${r2(cx + r * Math.cos(a))},${r2(cy + r * Math.sin(a))}`);
  }
  return "M" + pts.join("L") + "Z";
}

function renderBoard() {
  svg.innerHTML = "";
  svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);

  // 网格
  const step = 100;
  const gx0 = Math.floor(view.x / step) * step;
  const gy0 = Math.floor(view.y / step) * step;
  for (let x = gx0; x <= view.x + view.w; x += step) {
    el("line", { x1: x, y1: view.y, x2: x, y2: view.y + view.h, class: "grid-line" }, svg);
  }
  for (let y = gy0; y <= view.y + view.h; y += step) {
    el("line", { x1: view.x, y1: y, x2: view.x + view.w, y2: y, class: "grid-line" }, svg);
  }
  // 背景事件层
  el("rect", {
    x: view.x - 5000, y: view.y - 5000, width: view.w + 10000, height: view.h + 10000,
    fill: "transparent", "data-action": "bg",
  }, svg);

  const wps = state.route.waypoints;
  const halfW = num(state.route.corridorHalfWidth, 0.3) * num(state.route.unitScale, 100);
  const conflictNfz = new Set((analysis?.entries || []).map((e) => e.nfzId));

  // 禁飞区
  for (const nz of state.nfzs) {
    const v = nz.vertices || [];
    if (v.length >= 2) {
      const isSel = selection.type === "nfz" && selection.id === nz.id;
      let cls = "nfz-poly " + (isSel ? "selected" : "ok");
      el("polygon", {
        points: v.map((p) => `${r2(p[0])},${r2(p[1])}`).join(" "),
        class: cls,
        fill: nz.enabled === false ? "rgba(150,160,180,.05)"
          : conflictNfz.has(nz.id) ? "rgba(255,77,109,.10)" : "rgba(255,255,255,.05)",
        stroke: nz.enabled === false ? "#5b6880" : conflictNfz.has(nz.id) ? "#ff4d6d"
          : isSel ? "#ffd166" : "#8fa3c2",
        "data-action": "nfz-pick", "data-id": nz.id,
      }, svg);
      const cx = v.reduce((s, p) => s + p[0], 0) / v.length;
      const cy = v.reduce((s, p) => s + p[1], 0) / v.length;
      el("text", { x: r2(cx), y: r2(cy - 4), "text-anchor": "middle", class: "nfz-label", text: nz.name }, svg);
      el("text", { x: r2(cx), y: r2(cy + 10), "text-anchor": "middle", class: "nfz-label",
        text: `${num(nz.minAlt)}–${num(nz.maxAlt)}m` }, svg);
      if (isSel) {
        const pr = Math.max(6, 7 * screenScale());
        v.forEach((p, i) => {
          el("circle", { cx: r2(p[0]), cy: r2(p[1]), r: r2(pr), class: "nfz-vertex",
            "data-action": "nfz-vertex", "data-id": nz.id, "data-i": i }, svg);
        });
      }
    }
  }

  // 航线扫掠走廊（半宽描边近似缓冲区）
  if (wps.length >= 2) {
    const pts = wps.map((w) => `${r2(num(w.x))},${r2(num(w.y))}`).join(" ");
    el("polyline", { points: pts, class: "route-corridor",
      "stroke-width": r2(halfW * 2), "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    el("polyline", { points: pts, class: "route-line" }, svg);
  }

  // 冲突区间（沿航段的红色粗线段）与见证位置
  for (const e of analysis?.entries || []) {
    const a = wps[e.segIndex], b = wps[e.segIndex + 1];
    if (!a || !b) continue;
    const xa = num(a.x) + (num(b.x) - num(a.x)) * e.enterFrac;
    const ya = num(a.y) + (num(b.y) - num(a.y)) * e.enterFrac;
    const xb = num(a.x) + (num(b.x) - num(a.x)) * e.exitFrac;
    const yb = num(a.y) + (num(b.y) - num(a.y)) * e.exitFrac;
    el("line", { x1: r2(xa), y1: r2(ya), x2: r2(xb), y2: r2(yb),
      class: "conflict-overlay", "stroke-width": r2(halfW * 2), "stroke-linecap": "butt" }, svg);
  }
  const first = analysis?.first;
  if (first) {
    const w = first.witness, r = Math.max(9, 11 * screenScale());
    el("circle", { cx: w.x, cy: w.y, r: r2(r * 1.7), class: "witness-ring" }, svg);
    el("path", { d: starPath(w.x, w.y, r, r * 0.45), class: "witness" }, svg);
    el("text", { x: r2(w.x + 14 * screenScale()), y: r2(w.y - 14 * screenScale()),
      class: "witness-label", text: `首个冲突 ${fmtTime(first.enterTime)}` }, svg);
  }

  // 航点
  const pr = Math.max(6, 7 * screenScale());
  wps.forEach((w, i) => {
    const isSel = selection.type === "wp" && selection.id === w.id;
    el("circle", { cx: r2(num(w.x)), cy: r2(num(w.y)), r: r2(pr),
      class: "wp-dot" + (isSel ? " selected" : ""),
      "data-action": "wp-drag", "data-id": w.id }, svg);
    el("text", { x: r2(num(w.x) + 10 * screenScale()), y: r2(num(w.y) - 10 * screenScale()),
      class: "wp-label", text: `${i + 1}. ${w.name || "WP"} ${num(w.alt)}m` }, svg);
  });

  // 绘制预览
  if (mode === "drawNfz" && drawing.length) {
    const pts = drawing.map((p) => `${r2(p[0])},${r2(p[1])}`);
    if (hover) pts.push(`${r2(hover.x)},${r2(hover.y)}`);
    if (pts.length >= 2) el("polyline", { points: pts.join(" "), class: "draw-preview" }, svg);
    drawing.forEach((p) => el("circle", { cx: p[0], cy: p[1], r: r2(pr), class: "nfz-vertex" }, svg));
  }
}

// ----------------------------------------------------------- 视图操作
function fitView() {
  const pts = [];
  state.route.waypoints.forEach((w) => pts.push([num(w.x), num(w.y)]));
  state.nfzs.forEach((n) => (n.vertices || []).forEach((p) => pts.push([num(p[0]), num(p[1])])));
  if (!pts.length) { view = { x: 0, y: 0, w: 1000, h: 620 }; renderBoard(); return; }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of pts) { minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); }
  const pad = 90;
  view = { x: minX - pad, y: minY - pad, w: Math.max(maxX - minX + pad * 2, 200), h: Math.max(maxY - minY + pad * 2, 200) };
  renderBoard();
}

// ----------------------------------------------------------- 侧栏
function renderPanel() {
  panelDirty = false;
  const r = state.route;
  $("#depTime").value = r.departureTime || "";
  $("#corridorW").value = r.corridorHalfWidth ?? 0.3;
  $("#unitScale").value = r.unitScale ?? 100;

  // 航点卡片
  const wpBox = $("#wpList");
  wpBox.innerHTML = "";
  r.waypoints.forEach((w, i) => {
    const card = document.createElement("div");
    card.className = "wp-card" + (selection.type === "wp" && selection.id === w.id ? " selected" : "");
    card.innerHTML = `
      <div class="card-head">
        <b>${i + 1}.</b>
        <input data-f="name" value="${(w.name || "").replace(/"/g, "&quot;")}" style="flex:1;background:#0e1726;color:#d7e0ee;border:1px solid #31415c;border-radius:4px;padding:2px 6px;font-size:12px">
        <button class="del-btn" title="删除">✕</button>
      </div>
      <div class="form-row"><label>x / y</label>
        <input data-f="x" type="number" value="${w.x ?? 0}">
        <input data-f="y" type="number" value="${w.y ?? 0}">
      </div>
      <div class="form-row"><label>高度 m</label>
        <input data-f="alt" type="number" value="${w.alt ?? 0}">
      </div>`;
    card.querySelector(".del-btn").onclick = () => { deleteWaypoint(i); };
    card.querySelectorAll("input").forEach((inp) => {
      inp.oninput = () => {
        const f = inp.dataset.f;
        w[f] = f === "name" ? inp.value : num(inp.value, w[f] ?? 0);
        renderBoard(); scheduleAnalyze(); scheduleSave();
      };
      inp.onfocus = () => { selection = { type: "wp", id: w.id }; renderBoard(); markPanelDirty(); };
    });
    wpBox.appendChild(card);

    if (i < r.waypoints.length - 1) {
      const seg = document.createElement("div");
      seg.className = "seg-row";
      const spd = r.speeds[i] ?? 20;
      seg.innerHTML = `
        <span>→段${i + 1} 速度</span>
        <input type="number" step="1" value="${spd}">
        <span>km/h</span>
        <span class="seg-eta" data-seg="${i}"></span>`;
      const inp = seg.querySelector("input");
      inp.oninput = () => { r.speeds[i] = num(inp.value, 20); scheduleAnalyze(); scheduleSave(); };
      wpBox.appendChild(seg);
    }
  });

  // 禁飞区卡片
  const nfzBox = $("#nfzList");
  nfzBox.innerHTML = "";
  state.nfzs.forEach((nz, i) => {
    const card = document.createElement("div");
    card.className = "nfz-card" + (selection.type === "nfz" && selection.id === nz.id ? " selected" : "");
    card.innerHTML = `
      <div class="card-head">
        <input type="checkbox" ${nz.enabled !== false ? "checked" : ""} title="启用">
        <input data-f="name" value="${(nz.name || "").replace(/"/g, "&quot;")}" style="flex:1;background:#0e1726;color:#d7e0ee;border:1px solid #31415c;border-radius:4px;padding:2px 6px;font-size:12px">
        <button class="del-btn" title="删除">✕</button>
      </div>
      <div class="form-row"><label>高度层 m</label>
        <input data-f="minAlt" type="number" value="${nz.minAlt ?? 0}">
        <input data-f="maxAlt" type="number" value="${nz.maxAlt ?? 300}">
      </div>
      <div class="form-row"><label>生效起</label>
        <input data-f="startTime" type="datetime-local" step="60" value="${nz.startTime || ""}"></div>
      <div class="form-row"><label>生效止</label>
        <input data-f="endTime" type="datetime-local" step="60" value="${nz.endTime || ""}"></div>
      <div class="summary">${(nz.vertices || []).length} 个顶点${selection.type === "nfz" && selection.id === nz.id ? "（黄色顶点可在画布拖动）" : ""}</div>`;
    card.querySelector(".del-btn").onclick = () => { state.nfzs.splice(i, 1); afterStructuralChange(); };
    card.querySelector('input[type="checkbox"]').onchange = (e) => {
      nz.enabled = e.target.checked; renderBoard(); scheduleAnalyze(); scheduleSave();
    };
    card.querySelectorAll('input:not([type="checkbox"])').forEach((inp) => {
      inp.oninput = () => {
        const f = inp.dataset.f;
        nz[f] = f === "name" || f === "startTime" || f === "endTime" ? inp.value : num(inp.value);
        renderBoard(); scheduleAnalyze(); scheduleSave();
      };
      inp.onfocus = () => { selection = { type: "nfz", id: nz.id }; renderBoard(); markPanelDirty(); };
    });
    nfzBox.appendChild(card);
  });

  renderDynamicPanel();
}

// 仅刷新文本，不重建表单（避免输入失焦）
function renderDynamicPanel() {
  const r = state.route;
  const info = analysis?.route;
  $("#routeSummary").innerHTML = info
    ? `航段数 ${(r.waypoints.length - 1) || 0} ｜ 总航程 ${info.length} ｜ 总时长 ${info.durationMinutes ?? "—"} 分钟` : "";
  (document.querySelectorAll(".seg-eta") || []).forEach((span) => {
    const i = num(span.dataset.seg);
    const d = info?.distances?.[i], t = info?.times?.[i + 1];
    span.textContent = `${d != null ? d + "km" : ""} ETA ${fmtTime(t)}`;
  });
}

function markPanelDirty() { panelDirty = true; }
function refreshPanelIfDirty() { if (panelDirty) renderPanel(); }

function renderResults() {
  const box = $("#resultBox");
  if (!analysis) { box.innerHTML = ""; return; }
  let html = "";
  for (const er of analysis.errors || []) {
    html += `<div class="error-line">⚠ ${er.message}</div>`;
  }
  if (analysis.first) {
    const f = analysis.first;
    html += `<div class="result-bad result-first">
      <div class="big">🚫 首个冲突：${f.nfzName}</div>
      <div>航段 ${f.segIndex + 1} ｜ ${fmtTime(f.enterTime)} → ${fmtTime(f.exitTime)}（${f.durationMinutes ?? 0} 分钟）</div>
      <div>高度重叠 ${f.altLow}–${f.altHigh} m</div>
      <div>见证位置 (${f.witness.x}, ${f.witness.y})，高度 ${f.witness.alt} m</div>
    </div>`;
    html += (analysis.entries || []).map((e, i) => `
      <div class="entry-line ${i === 0 ? "first" : ""}">
        ${i === 0 ? "★ " : ""}${e.nfzName} · 航段${e.segIndex + 1} ·
        ${fmtTime(e.enterTime)}→${fmtTime(e.exitTime)} ·
        高度 ${e.altLow}–${e.altHigh}m · 见证 (${e.witness.x},${e.witness.y})
      </div>`).join("");
  } else if (!(analysis.errors || []).length) {
    html += `<div class="result-good">✅ 未发现平面 ∩ 高度 ∩ 时窗的三维冲突</div>`;
  }
  box.innerHTML = html;
}

// ----------------------------------------------------------- 结构变更
function afterStructuralChange() {
  renderBoard(); renderPanel(); scheduleAnalyze(); scheduleSave();
}
function deleteWaypoint(i) {
  const r = state.route;
  r.waypoints.splice(i, 1);
  if (i < r.speeds.length) r.speeds.splice(i, 1);
  if (selection.type === "wp") selection = { type: null, id: null };
  afterStructuralChange();
}
function appendWaypoint(w) {
  const r = state.route;
  r.waypoints.push(w);
  if (r.speeds.length < r.waypoints.length - 1) r.speeds.push(20);
  afterStructuralChange();
}

// ----------------------------------------------------------- 画布交互
svg.addEventListener("mousedown", (e) => {
  const t = e.target.closest("[data-action]");
  const action = t?.dataset.action;
  const w = toWorld(e.clientX, e.clientY);

  if (action === "wp-drag") {
    const id = t.dataset.id;
    const wp = state.route.waypoints.find((x) => x.id === id);
    selection = { type: "wp", id };
    drag = { kind: "wp", wp, startX: e.clientX, startY: e.clientY, moved: false };
    refreshPanelIfDirty(); renderBoard();
  } else if (action === "nfz-vertex") {
    const nz = state.nfzs.find((n) => n.id === t.dataset.id);
    drag = { kind: "vertex", nz, i: +t.dataset.i, startX: e.clientX, startY: e.clientY, moved: false };
  } else if (action === "nfz-pick") {
    selection = { type: "nfz", id: t.dataset.id };
    markPanelDirty(); renderBoard();
  } else {
    // 背景：开始平移
    drag = { kind: "pan", view: { ...view }, startX: e.clientX, startY: e.clientY };
  }
});

svg.addEventListener("mousemove", (e) => {
  const w = toWorld(e.clientX, e.clientY);
  hover = w;
  if (!drag) {
    if (mode === "drawNfz" && drawing.length) renderBoard();
    return;
  }
  const dxPx = e.clientX - drag.startX, dyPx = e.clientY - drag.startY;
  if (Math.abs(dxPx) + Math.abs(dyPx) > 3) drag.moved = true;

  if (drag.kind === "pan") {
    const rect = svg.getBoundingClientRect();
    view.x = drag.view.x - (dxPx / rect.width) * view.w;
    view.y = drag.view.y - (dyPx / rect.height) * view.h;
    renderBoard();
  } else if (drag.kind === "wp") {
    drag.wp.x = r2(w.x); drag.wp.y = r2(w.y);
    renderBoard();
  } else if (drag.kind === "vertex") {
    drag.nz.vertices[drag.i] = [r2(w.x), r2(w.y)];
    renderBoard();
  }
});

window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  const wasClick = !drag.moved;
  const d = drag;
  drag = null;
  if (wasClick && d.kind === "pan") {
    const w = toWorld(e.clientX, e.clientY);
    if (mode === "addWp") {
      appendWaypoint({ id: uid("w"), name: "WP", x: r2(w.x), y: r2(w.y), alt: 200 });
    } else if (mode === "drawNfz") {
      drawing.push([r2(w.x), r2(w.y)]);
      renderBoard();
    } else {
      selection = { type: null, id: null };
      markPanelDirty(); renderBoard();
    }
  } else if (d.kind === "wp" || d.kind === "vertex") {
    if (d.moved) { scheduleAnalyze(); scheduleSave(); }
  }
});

svg.addEventListener("dblclick", (e) => {
  if (mode === "drawNfz" && drawing.length >= 3) {
    // 双击的第一次 click 已落下同位置末点，去重
    if (typeof e.clientX === "number") {
      const w = toWorld(e.clientX, e.clientY);
      const last = drawing[drawing.length - 1];
      if (Math.hypot(last[0] - w.x, last[1] - w.y) < 5) drawing.pop();
    }
    const verts = drawing.map((p) => [r2(p[0]), r2(p[1])]);
    if (verts.length >= 3) {
      state.nfzs.push({
        id: uid("n"), name: `禁飞区-${state.nfzs.length + 1}`, vertices: verts,
        minAlt: 0, maxAlt: 300, startTime: "", endTime: "", enabled: true,
      });
      drawing = [];
      afterStructuralChange();
    }
  }
});

svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const w = toWorld(e.clientX, e.clientY);
  const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
  view = {
    x: w.x - (w.x - view.x) * factor,
    y: w.y - (w.y - view.y) * factor,
    w: view.w * factor, h: view.h * factor,
  };
  renderBoard();
}, { passive: false });

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (drawing.length) { drawing = []; renderBoard(); }
    else { setMode("select"); }
  } else if (e.key === "Enter" && mode === "drawNfz" && drawing.length >= 3) {
    svg.dispatchEvent(new Event("dblclick"));
  }
});

// ----------------------------------------------------------- 顶部控件
function setMode(m) {
  mode = m;
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
  svg.style.cursor = m === "select" ? "default" : "crosshair";
  const hints = {
    select: "选择/拖动：拖动航点与禁飞区顶点重算；拖空白平移；滚轮缩放",
    addWp: "点击画布追加航点（Esc 结束）",
    drawNfz: "单击依次落点，双击或回车完成禁飞多边形（Esc 取消）",
  };
  $("#modeHint").textContent = hints[m];
  if (m !== "drawNfz") drawing = [];
  renderBoard();
}
document.querySelectorAll(".mode").forEach((b) => b.onclick = () => setMode(b.dataset.mode));
$("#btnFit").onclick = fitView;
$("#btnAnalyze").onclick = runAnalyze;

$("#depTime").oninput = (e) => { state.route.departureTime = e.target.value; scheduleAnalyze(); scheduleSave(); };
$("#corridorW").oninput = (e) => { state.route.corridorHalfWidth = num(e.target.value, 0.3); renderBoard(); scheduleAnalyze(); scheduleSave(); };
$("#unitScale").oninput = (e) => { state.route.unitScale = num(e.target.value, 100); renderBoard(); scheduleAnalyze(); scheduleSave(); };

$("#btnAddNfz").onclick = () => {
  const cx = view.x + view.w / 2, cy = view.y + view.h / 2, s = Math.min(view.w, view.h) / 8;
  state.nfzs.push({
    id: uid("n"), name: `禁飞区-${state.nfzs.length + 1}`,
    vertices: [[r2(cx - s), r2(cy - s)], [r2(cx + s), r2(cy - s)], [r2(cx + s), r2(cy + s)], [r2(cx - s), r2(cy + s)]],
    minAlt: 0, maxAlt: 300, startTime: "", endTime: "", enabled: true,
  });
  selection = { type: "nfz", id: state.nfzs[state.nfzs.length - 1].id };
  afterStructuralChange();
};

// ----------------------------------------------------------- 版本
async function loadVersions() {
  const list = await api("/api/versions");
  const box = $("#versionList");
  box.innerHTML = "";
  if (!list.length) { box.innerHTML = '<div class="summary">暂无保存版本</div>'; return; }
  for (const v of list) {
    const item = document.createElement("div");
    item.className = "version-item";
    item.innerHTML = `<div class="vmeta"><div class="vname"></div><div class="vtime"></div></div>
      <button data-act="load">载入</button><button data-act="del">删除</button>`;
    item.querySelector(".vname").textContent = v.name;
    item.querySelector(".vtime").textContent = v.createdAt;
    item.querySelector('[data-act="load"]').onclick = async () => {
      if (!confirm(`载入版本「${v.name}」？当前未另存的内容将被覆盖。`)) return;
      const full = await api(`/api/versions/${v.id}`);
      Object.assign(state, full.state);
      selection = { type: null, id: null };
      await api("/api/state", { method: "PUT", body: JSON.stringify(state) });
      renderPanel(); fitView(); runAnalyze();
      toast("已载入版本 " + v.name);
    };
    item.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`删除版本「${v.name}」？`)) return;
      await api(`/api/versions/${v.id}`, { method: "DELETE" });
      loadVersions();
    };
    box.appendChild(item);
  }
}
$("#btnSaveVer").onclick = async () => {
  const name = $("#verName").value.trim();
  const r = await api("/api/versions", { method: "POST", body: JSON.stringify({ name, state }) });
  if (r.ok) { toast("已保存：" + r.name); $("#verName").value = ""; loadVersions(); }
};

// ----------------------------------------------------------- 启动
(async function init() {
  setMode("select");
  await loadState();
  renderPanel();
  fitView();
  loadVersions();
  runAnalyze();
})();
