"use strict";
/* Model Arena dashboard. Plain JS, no build step. State lives in `S`; every render reads from it.
   URL keeps split / view / active run / compared runs so a link reproduces the same screen. */

// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const minus = "−";
const sgn = v => (v > 0 ? "+" : v < 0 ? minus : "");
const pct = (v, d = 1) => v == null ? "–" : (v * 100).toFixed(d) + "%";
const spct = (v, d = 2) => v == null ? "–" : sgn(v) + Math.abs(v * 100).toFixed(d) + "%";
const num = (v, d = 2) => v == null ? "–" : (v < 0 ? minus : "") + Math.abs(v).toFixed(d);
const pp = (v, d = 1) => v == null ? "–" : num(v, d) + "%p";
const shortHash = h => (h ? String(h).slice(0, 10) : "–");
const bytes = n => n == null ? "–" : n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`;
const when = iso => iso ? new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" }) : "–";

const FAM = { rule: "규칙", supervised: "지도학습", rl: "강화학습", baseline: "기준선" };
const SPLIT = { dev: "개발 구간", "dev-late": "개발 후반 구간", holdout: "시험 구간" };
const SLOTS = ["--series-1", "--series-2", "--series-3", "--series-4"];
const REPRO = {
  reproduced: ["pass", "재현됨"], partial: ["warn", "부분 확인"], not_reproduced: ["fail", "재현 실패"],
  unchecked: ["none", "미확인"], missing: ["none", "기록 없음"],
};

let D = null;
const S = {
  track: null, split: null, view: "board", fams: new Set(Object.keys(FAM)), gatesOnly: false, q: "",
  sort: { key: "mean_return", dir: -1 }, selected: new Map(), active: null, tab: "perf",
  sx: "max_drawdown_pp", sy: "mean_return",
};

// ---------- data access ----------
const inSplit = () => D.runs.filter(r => r.run.track_id === S.track && r.run.split === S.split);
const baselines = () => inSplit().filter(r => r.run.model.family === "baseline");
const byId = id => D.runs.find(r => r.run.run_id === id);
const reproState = r => r.reproduction ? r.reproduction.verdict : r.run.provenance ? "unchecked" : "missing";
const colorOf = id => S.selected.has(id) ? css(SLOTS[S.selected.get(id)]) : null;
const selectedRuns = () => [...S.selected.entries()].sort((a, b) => a[1] - b[1]).map(([id]) => byId(id)).filter(Boolean);

function band(key = "mean_return") {
  const v = baselines().map(b => b.metrics[key]).filter(x => x != null);
  return v.length ? [Math.min(...v), Math.max(...v)] : null;
}
function baseMean() {
  const v = baselines().map(b => b.metrics.mean_return).filter(x => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}
function visibleRuns() {
  const q = S.q.trim().toLowerCase(), bm = baseMean();
  const rows = inSplit().filter(r => S.fams.has(r.run.model.family)
    && (!S.gatesOnly || r.metrics.passes_gates)
    && (!q || `${r.run.run_id} ${r.run.model.name}`.toLowerCase().includes(q)));
  rows.forEach(r => { r.metrics.vs_base = bm == null || r.metrics.mean_return == null ? null : r.metrics.mean_return - bm; });
  const col = COLUMNS.find(c => c.key === S.sort.key);
  const val = col && col.sort ? col.sort : r => r.metrics[S.sort.key];
  return rows.sort((a, b) => {
    const va = val(a), vb = val(b);
    if (typeof va === "string" || typeof vb === "string") return S.sort.dir * String(va).localeCompare(String(vb));
    return S.sort.dir * ((va ?? -Infinity) - (vb ?? -Infinity));
  });
}

// ---------- selection ----------
function toggleSelect(id) {
  if (S.selected.has(id)) S.selected.delete(id);
  else {
    const used = new Set(S.selected.values());
    const free = [0, 1, 2, 3].find(i => !used.has(i));
    if (free == null) { toast("비교는 4개까지입니다. 목록에서 하나를 빼세요."); renderBoard(); return; }
    S.selected.set(id, free); // the colour stays with the run until it is removed
  }
  renderAll();
}
function defaultPick() {
  S.selected.clear();
  const c = inSplit().filter(r => r.run.model.family !== "baseline").sort((a, b) => b.metrics.mean_return - a.metrics.mean_return);
  const ref = c.find(r => r.run.run_id.startsWith("sel-v2.current.after_tp3.sys"));
  const rl = c.find(r => r.run.model.family === "rl" && r.metrics.passes_gates) || c.find(r => r.run.model.family === "rl");
  [...new Set([rl, ref, c.find(r => r !== rl && r !== ref)].filter(Boolean))].slice(0, 3).forEach((r, i) => S.selected.set(r.run.run_id, i));
}

// ---------- URL state ----------
function writeUrl() {
  const p = new URLSearchParams();
  p.set("split", S.split);
  if (S.view !== "board") p.set("view", S.view);
  if (S.active) p.set("run", S.active);
  if (S.selected.size) p.set("cmp", selectedRuns().map(r => r.run.run_id).join(","));
  history.replaceState(null, "", `${location.pathname}?${p}`);
}
function readUrl() {
  const p = new URLSearchParams(location.search);
  return { split: p.get("split"), view: p.get("view"), run: p.get("run"), cmp: p.get("cmp") };
}

// ---------- tooltip / toast ----------
function showTip(html, e) {
  const t = $("#tip");
  t.innerHTML = html; t.style.display = "block";
  t.style.left = Math.min(e.clientX + 14, innerWidth - t.offsetWidth - 8) + "px";
  t.style.top = Math.min(e.clientY + 14, innerHeight - t.offsetHeight - 8) + "px";
}
const hideTip = () => { $("#tip").style.display = "none"; };
function bindTips(root, html) {
  $$("[data-tip]", root).forEach(el => {
    el.addEventListener("mousemove", e => showTip(html(el.dataset.tip, el), e));
    el.addEventListener("mouseleave", hideTip);
  });
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.role = "status"; t.textContent = msg;
  document.body.append(t); setTimeout(() => t.remove(), 2200);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("복사했습니다"); } catch { toast("복사하지 못했습니다. 직접 선택해 복사하세요."); }
}
const copyField = text => `<div class="copy"><code>${esc(text)}</code><button data-copy="${esc(text)}">복사</button></div>`;

// ---------- chart primitives ----------
function scale(d0, d1, r0, r1) { const k = (r1 - r0) / ((d1 - d0) || 1); return v => r0 + (v - d0) * k; }
function ticks(lo, hi, n = 5) {
  const raw = (hi - lo) / n || 1, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function extent(values, padRatio = 0.06) {
  const v = values.filter(x => x != null && Number.isFinite(x));
  let lo = Math.min(...v), hi = Math.max(...v);
  const pad = (hi - lo) * padRatio || Math.abs(hi) * 0.1 || 1;
  return [lo - pad, hi + pad];
}

// forest cell: one row of the leaderboard forest plot
function forestCell(r, dom, bnd, W = 200, H = 26) {
  const x = scale(dom[0], dom[1], 6, W - 6), m = r.metrics, [a, b] = m.mean_return_ci95;
  const c = colorOf(r.run.run_id) || "var(--ink-2)", y = H / 2;
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
    ${bnd ? `<rect x="${x(bnd[0])}" y="3" width="${Math.max(2, x(bnd[1]) - x(bnd[0]))}" height="${H - 6}" rx="2" fill="var(--band)" opacity=".55"/>` : ""}
    <line x1="${x(0)}" x2="${x(0)}" y1="1" y2="${H - 1}" stroke="var(--zero)" stroke-dasharray="2 2"/>
    ${a != null ? `<line x1="${x(a)}" x2="${x(b)}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="2" stroke-linecap="round"/>` : ""}
    ${m.mean_return != null ? `<circle cx="${x(m.mean_return)}" cy="${y}" r="4.5" fill="${c}" stroke="var(--panel)" stroke-width="2"/>` : ""}
  </svg>`;
}
function forestAxis(dom, W = 200) {
  const x = scale(dom[0], dom[1], 6, W - 6);
  return `<svg width="${W}" height="14" viewBox="0 0 ${W} 14" aria-hidden="true">${ticks(dom[0] * 100, dom[1] * 100, 3)
    .map(t => `<text x="${x(t / 100)}" y="11" text-anchor="middle">${t === 0 ? "0" : sgn(t) + Math.abs(t)}%</text>`).join("")}</svg>`;
}
function sparkline(r, W = 92, H = 24) {
  const pts = r.metrics.curve;
  if (!pts || pts.length < 2) return "";
  const vals = pts.map(p => p[1]), [lo, hi] = [Math.min(0, ...vals), Math.max(0, ...vals)];
  const x = scale(0, pts.length - 1, 2, W - 4), y = scale(lo, hi, H - 3, 3);
  const c = colorOf(r.run.run_id) || "var(--ink-3)";
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">
    <line x1="2" x2="${W - 2}" y1="${y(0)}" y2="${y(0)}" stroke="var(--rule)"/>
    <path d="${pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("")}" fill="none" stroke="${c}" stroke-width="1.5"/>
    <circle cx="${x(pts.length - 1)}" cy="${y(vals.at(-1))}" r="2.5" fill="${c}"/>
  </svg>`;
}
const reproBadge = r => { const [cls, label] = REPRO[reproState(r)]; return `<span class="badge ${cls}">${label}</span>`; };
const gateBadge = r => r.metrics.passes_gates ? `<span class="badge pass">충족</span>` : `<span class="badge fail">미달</span>`;
const famTag = r => `<span class="tag ${r.run.model.family}">${FAM[r.run.model.family]}</span>`;
const swatch = id => S.selected.has(id) ? `<span class="swatch" style="background:${colorOf(id)}">${S.selected.get(id) + 1}</span>` : "";

// ---------- leaderboard ----------
const COLUMNS = [
  { key: "select", label: "<span class='sr-only'>비교</span>", cls: "check", nosort: true },
  { key: "run", label: "run", cls: "l", sort: r => r.run.run_id },
  { key: "trades", label: "거래", sort: r => r.metrics.trades },
  { key: "accuracy", label: "매매 정확도" },
  { key: "mean_return", label: "평균 순수익" },
  { key: "forest", label: "95% 구간과 기준선", cls: "forest-h", nosort: true },
  { key: "vs_base", label: "기준선 대비" },
  { key: "profit_factor", label: "PF" },
  { key: "max_drawdown_pp", label: "MDD" },
  { key: "curve", label: "누적", nosort: true },
  { key: "repro", label: "재현", sort: r => ["missing", "unchecked", "not_reproduced", "partial", "reproduced"].indexOf(reproState(r)) },
  { key: "gates", label: "기준", sort: r => (r.metrics.passes_gates ? 1 : 0) },
];

function renderBoard() {
  const rows = visibleRuns(), bnd = band();
  const ciVals = rows.flatMap(r => [...r.metrics.mean_return_ci95, r.metrics.mean_return]).concat(bnd || [], [0]);
  const dom = extent(ciVals, 0.04);
  $("#board thead").innerHTML = `<tr>${COLUMNS.map(c => {
    const sorted = S.sort.key === c.key ? ` aria-sort="${S.sort.dir > 0 ? "ascending" : "descending"}"` : "";
    if (c.nosort) return `<th class="${c.cls || ""}" scope="col">${c.key === "forest" ? `<div style="padding:6px 8px 2px">${c.label}</div>${forestAxis(dom)}` : `<div style="padding:9px 10px">${c.label}</div>`}</th>`;
    return `<th class="${c.cls || ""}" scope="col"${sorted}><button data-sort="${c.key}">${c.label}</button></th>`;
  }).join("")}</tr>`;
  $("#board tbody").innerHTML = rows.length ? rows.map(r => {
    const m = r.metrics, id = r.run.run_id, active = S.active === id;
    const vb = m.vs_base;
    return `<tr data-id="${esc(id)}" tabindex="0" ${active ? 'aria-current="true"' : ""}>
      <td class="check"><input type="checkbox" ${S.selected.has(id) ? "checked" : ""} aria-label="${esc(id)} 비교에 추가"></td>
      <td class="l"><div class="name">${swatch(id)}<span class="id" title="${esc(id)}">${esc(id)}</span>${famTag(r)}</div></td>
      <td>${m.trades}${m.unclosed ? `<div class="sub">미청산 ${m.unclosed}</div>` : ""}</td>
      <td>${pct(m.accuracy)}<div class="sub">${pct(m.accuracy_ci95[0], 0)}–${pct(m.accuracy_ci95[1], 0)}</div></td>
      <td class="num-strong">${spct(m.mean_return, 3)}</td>
      <td class="forest" data-tip="${esc(id)}">${forestCell(r, dom, bnd)}</td>
      <td class="${vb != null && vb < 0 ? "below" : ""}">${vb == null ? "–" : sgn(vb) + Math.abs(vb * 100).toFixed(2) + "%p"}</td>
      <td>${num(m.profit_factor)}</td>
      <td>${pp(m.max_drawdown_pp)}</td>
      <td>${sparkline(r)}</td>
      <td>${reproBadge(r)}</td>
      <td>${gateBadge(r)}</td></tr>`;
  }).join("") : `<tr><td colspan="${COLUMNS.length}"><div class="empty"><b>조건에 맞는 run이 없습니다</b>필터를 줄이거나 다른 구간을 선택하세요.</div></td></tr>`;
  $("#rowCount").textContent = `${rows.length}개 표시`;
  bindTips($("#board tbody"), id => {
    const r = byId(id), m = r.metrics, b = band();
    return `<div class="t">${esc(id)}</div><div class="row"><span>평균 순수익</span><b>${spct(m.mean_return, 3)}</b></div>
      <div class="row"><span>95% 구간</span><span>${spct(m.mean_return_ci95[0])} … ${spct(m.mean_return_ci95[1])}</span></div>
      ${b ? `<div class="row"><span>기준선 범위</span><span>${spct(b[0])} … ${spct(b[1])}</span></div>` : ""}`;
  });
}

// ---------- summary ----------
function renderSummary() {
  const t = D.tracks[S.track], w = t.windows[S.split] || {};
  const models = inSplit().filter(r => r.run.model.family !== "baseline"), b = band();
  let head;
  if (!models.length) head = `${SPLIT[S.split] || S.split}에 제출된 run이 아직 없습니다.`;
  else if (!b) head = `${models.length}개 run을 비교합니다. 이 구간에는 무작위 기준선이 없습니다.`;
  else {
    const sure = models.filter(r => r.metrics.mean_return_ci95[0] > b[1]).length;
    const above = models.filter(r => r.metrics.mean_return > b[1]).length;
    head = sure ? `${models.length}개 run 중 ${sure}개가 신뢰구간까지 무작위 기준선을 앞섭니다.`
      : above ? `${models.length}개 run 중 ${above}개가 평균으로는 기준선보다 높지만, 신뢰구간까지 앞선 run은 없습니다.`
      : `${models.length}개 run 모두 무작위 기준선 범위 안이거나 그보다 낮습니다.`;
  }
  $("#headline").textContent = head;
  const caution = S.split === "holdout" ? "" : " 개발에 이미 노출된 기간이므로 최종 성능 근거로 쓰지 않습니다.";
  $("#windowNote").textContent = `${SPLIT[S.split] || S.split} ${w.start || "?"} ~ ${w.end || "미정"}. ${w.note || ""}${caution}`;
  $("#splits").innerHTML = Object.keys(t.windows).map(s => {
    const n = D.runs.filter(r => r.run.track_id === S.track && r.run.split === s).length;
    return `<button data-split="${s}" aria-pressed="${s === S.split}">${esc(s)}<span class="n">${n}</span></button>`;
  }).join("");
  $("#families").innerHTML = Object.entries(FAM).map(([k, label]) => {
    const n = inSplit().filter(r => r.run.model.family === k).length;
    return `<button class="toggle" data-fam="${k}" aria-pressed="${S.fams.has(k)}"><span class="dot"></span>${label} ${n}</button>`;
  }).join("");
  $("#gatesOnly").setAttribute("aria-pressed", S.gatesOnly);
}

// ---------- inspector ----------
function histogram(m) {
  const e = m.return_hist.edges.map(v => Math.round(v * 100)), counts = m.return_hist.counts;
  const labels = counts.map((_, i) => i === 0 ? `${e[0]}% 미만` : i === counts.length - 1 ? `${e.at(-1)}% 이상` : `${e[i - 1]}~${e[i]}%`);
  const W = 400, H = 150, max = Math.max(...counts, 1), bw = (W - 8) / counts.length, y = scale(0, max, H - 26, 16), z = e.indexOf(0);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="거래별 순수익률 분포">
    ${counts.map((c, i) => {
      const x0 = 4 + i * bw + 1, top = y(c), hh = H - 26 - top;
      const path = c ? `<path d="M${x0},${H - 26}V${top + Math.min(3, hh)}q0,-3 3,-3h${bw - 8}q3,0 3,3V${H - 26}Z" fill="${i <= z ? "var(--band)" : "var(--series-1)"}"/>` : "";
      return `<g data-tip="${labels[i]}: ${c}건"><rect x="${x0}" y="0" width="${bw - 2}" height="${H - 20}" fill="transparent"/>${path}${c ? `<text x="${x0 + (bw - 2) / 2}" y="${top - 4}" text-anchor="middle">${c}</text>` : ""}</g>`;
    }).join("")}
    <line x1="4" x2="${W - 4}" y1="${H - 26}" y2="${H - 26}" stroke="var(--zero)"/>
    <line x1="${4 + (z + 1) * bw}" x2="${4 + (z + 1) * bw}" y1="10" y2="${H - 20}" stroke="var(--zero)" stroke-dasharray="2 2"/>
    <text x="${4 + (z + 1) * bw}" y="${H - 8}" text-anchor="middle">0%</text>
    <text x="4" y="${H - 8}">${labels[0]}</text><text x="${W - 4}" y="${H - 8}" text-anchor="end">${labels.at(-1)}</text>
  </svg>`;
}
function trainCurve(curve) {
  const W = 400, H = 130, v = curve.map(c => c.value), [lo, hi] = [Math.min(0, ...v), Math.max(0, ...v)];
  const x = scale(0, curve.length - 1, 34, W - 6), y = scale(lo, hi, H - 20, 8);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="학습 곡선">
    <g class="axis">${ticks(lo, hi, 3).map(t => `<line x1="34" x2="${W - 6}" y1="${y(t)}" y2="${y(t)}"/><text x="28" y="${y(t) + 4}" text-anchor="end">${t}</text>`).join("")}</g>
    <path d="${curve.map((c, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(c.value).toFixed(1)}`).join("")}" fill="none" stroke="var(--series-1)" stroke-width="2"/>
    <text x="34" y="${H - 4}">1</text><text x="${W - 6}" y="${H - 4}" text-anchor="end">${curve.length} step</text></svg>`;
}

function perfTab(r) {
  const m = r.metrics;
  const reasons = Object.entries(m.exit_reasons), maxR = Math.max(...reasons.map(([, v]) => v), 1);
  return `<div class="stats">
      <div class="stat"><div class="k">매매 정확도</div><div class="v">${pct(m.accuracy)}</div><div class="s">95% ${pct(m.accuracy_ci95[0])}–${pct(m.accuracy_ci95[1])}</div></div>
      <div class="stat"><div class="k">거래당 평균 순수익</div><div class="v">${spct(m.mean_return, 3)}</div><div class="s">중앙값 ${spct(m.median_return, 3)}</div></div>
      <div class="stat"><div class="k">평균 이익 / 손실</div><div class="v" style="font-size:16px">${spct(m.mean_win)} / ${spct(m.mean_loss)}</div><div class="s">최고 ${spct(m.best_trade)}, 최악 ${spct(m.worst_trade)}</div></div>
      <div class="stat"><div class="k">최대 낙폭</div><div class="v">${pp(m.max_drawdown_pp)}</div><div class="s">거래 ${m.trades}건, 활동일 ${m.active_days}일</div></div>
    </div>
    <h3>거래별 순수익률 분포</h3><div class="hist">${histogram(m)}</div>
    <h3>청산 사유</h3>
    <table class="rows">${reasons.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="bar-cell"><span style="width:${(v / maxR) * 100}%"></span></td><td>${v}건 (${pct(v / m.trades)})</td></tr>`).join("")}</table>
    ${r.train_curve ? `<h3>학습 곡선</h3><p class="note">${esc(r.train_curve[0].metric || "value")}, 학습 구간 기준</p>${trainCurve(r.train_curve)}` : ""}`;
}

function reproTab(r) {
  const run = r.run, p = run.provenance, rep = r.reproduction, id = run.run_id;
  const cmd = `uv run python -m arena reproduce ${id}`;
  let out = "";
  if (rep) {
    const [cls, label] = REPRO[rep.verdict];
    out += `<p class="note"><span class="badge ${cls}">${label}</span> ${when(rep.checked_at)}, ${esc(rep.checked_by)} 확인</p>
      <ul class="checks">${rep.checks.map(c => `<li><span class="ic ${c.status}">${{ pass: "✓", fail: "✕", warn: "!", skip: "–" }[c.status]}</span>
        <div><div class="name">${{ inputs: "입력 데이터", artifact: "저장된 모델로 재생", retrain: "커밋에서 다시 학습", clean_commit: "커밋 상태", provenance: "재현 정보" }[c.name] || esc(c.name)}</div><div class="detail">${esc(c.detail)}</div></div></li>`).join("")}</ul>`;
  } else if (p) {
    out += `<div class="callout info">아직 재현을 확인하지 않았습니다. 아래 명령으로 확인하면 결과가 여기에 표시됩니다.</div>`;
  }
  if (!p) return out + `<div class="callout">이 run에는 재현 정보가 없습니다. 같은 명령으로 다시 만들어 제출하면 코드 버전, 데이터 해시, 모델 파일이 함께 기록됩니다.</div>`;
  out += `<h3>재현 확인</h3>${copyField(cmd)}`;
  const code = p.code || {};
  const commitLink = code.repo && code.commit ? `<a href="${esc(code.repo)}/commit/${esc(code.commit)}" target="_blank" rel="noopener">${esc(code.commit.slice(0, 10))}</a>` : esc(shortHash(code.commit));
  out += `<h3>코드</h3>
    ${code.dirty ? `<div class="callout">만들 당시 커밋되지 않은 변경이 있었습니다: ${esc((code.dirty_files || []).slice(0, 4).join(", "))}. 커밋만으로는 같은 결과가 나오지 않을 수 있습니다.</div>` : ""}
    <dl class="kv"><dt>커밋</dt><dd>${commitLink}</dd><dt>브랜치</dt><dd>${esc(code.branch || "–")}</dd></dl>
    <h3>다시 만드는 명령</h3>
    ${code.commit ? copyField(`git checkout ${code.commit}`) : ""}${copyField(`uv run python -m arena ${(p.command || []).join(" ")}`)}`;
  if ((run.artifacts || []).length) {
    out += `<h3>모델 파일</h3><ul class="files">${run.artifacts.map(a => `<li><span>${esc(a.name)}</span><span class="h">${bytes(a.bytes)}, ${shortHash(a.sha256)}</span></li>`).join("")}</ul>
      ${D.store ? copyField(`aws s3 cp ${D.store}/runs/${id}/artifacts/ ./${id}/ --recursive`) : ""}`;
  }
  if ((p.inputs || []).length) {
    out += `<h3>입력 데이터</h3><ul class="files">${p.inputs.map(i => `<li><span>${esc(i.path.split("/").slice(-2).join("/"))}</span><span class="h">${bytes(i.bytes)}, ${shortHash(i.sha256)}</span></li>`).join("")}</ul>`;
  }
  const env = p.env || {};
  out += `<h3>실행 환경</h3><dl class="kv"><dt>Python</dt><dd>${esc(env.python)} (${esc(env.platform)})</dd>
    <dt>패키지</dt><dd>${esc(Object.entries(env.packages || {}).map(([k, v]) => `${k} ${v}`).join(", "))}</dd>
    <dt>uv.lock</dt><dd>${shortHash(env.uv_lock_sha256)}</dd></dl>`;
  return out;
}

function infoTab(r) {
  const run = r.run, sub = r.submission;
  const params = Object.entries(run.params || {}).filter(([k]) => k !== "take_minus_skip_weights");
  return `${run.notes ? `<p class="note">${esc(run.notes)}</p>` : ""}
    <dl class="kv"><dt>모델</dt><dd>${esc(run.model.name)}</dd><dt>버전</dt><dd>${esc(run.model.version)}</dd>
    <dt>작성자</dt><dd>${esc(run.author)}</dd>
    <dt>제출</dt><dd>${sub ? `${esc(sub.submitted_by)}, ${when(sub.submitted_at)}${sub.enriched_at ? `<div class="sub">재현 정보 추가: ${esc(sub.enriched_by)}, ${when(sub.enriched_at)}</div>` : ""}` : "공유 저장소에 제출되지 않음"}</dd>
    <dt>생성</dt><dd>${when(run.created_at)}</dd>
    ${run.source ? `<dt>원 실험</dt><dd>${esc(run.source)}</dd>` : ""}
    <dt>모델 해시</dt><dd>${shortHash(run.artifact_hash)}</dd></dl>
    ${params.length ? `<h3>설정</h3><dl class="kv">${params.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) && v.length > 6 ? `${v.length}개: ${v.slice(0, 6).join(", ")} …` : typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}</dl>` : ""}
    <h3>판정 기준</h3><dl class="kv">${Object.entries(r.metrics.gates).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v ? "충족" : "미달"}</dd>`).join("")}</dl>`;
}

function renderInspector() {
  const r = S.active && byId(S.active), el = $("#inspector");
  $("#workbench").classList.toggle("with-inspector", !!r);
  if (!r) { el.hidden = true; return; }
  el.hidden = false;
  const tabs = [["perf", "성과"], ["repro", "재현"], ["info", "정보"]];
  el.innerHTML = `<header>
      <button class="btn icon ghost close" id="closeInspector" aria-label="상세 닫기">✕</button>
      <h2>${esc(r.run.run_id)}</h2>
      <div class="meta">${famTag(r)}<span class="tag">${esc(r.run.split)}</span>${gateBadge(r)}${reproBadge(r)}
        <button class="btn small ${S.selected.has(r.run.run_id) ? "" : "primary"}" id="inspectorSelect">${S.selected.has(r.run.run_id) ? "비교에서 빼기" : "비교에 추가"}</button></div>
    </header>
    <div class="tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" data-tab="${k}" aria-selected="${S.tab === k}">${l}</button>`).join("")}</div>
    <div class="tabpanel" role="tabpanel">${S.tab === "perf" ? perfTab(r) : S.tab === "repro" ? reproTab(r) : infoTab(r)}</div>`;
  bindTips(el, t => esc(t));
}

// ---------- compare view ----------
const METRICS = [
  { k: "mean_return", label: "거래당 평균 순수익", hint: "높을수록 좋음, 0 기준", fmt: v => spct(v, 3), ax: v => spct(v, 1), ci: m => m.mean_return_ci95, ref: 0, better: 1 },
  { k: "accuracy", label: "매매 정확도", hint: "높을수록 좋음", fmt: v => pct(v), ax: v => pct(v, 0), ci: m => m.accuracy_ci95, better: 1 },
  { k: "profit_factor", label: "Profit factor", hint: "높을수록 좋음, 1 기준", fmt: v => num(v), ax: v => num(v, 1), ref: 1, better: 1 },
  { k: "max_drawdown_pp", label: "최대 낙폭", hint: "0에 가까울수록 좋음", fmt: v => pp(v), ax: v => num(v, 0), ref: 0, better: 1 },
  { k: "win_rate", label: "승률", hint: "청산 거래 기준", fmt: v => pct(v), ax: v => pct(v, 0), better: 1 },
  { k: "sharpe_daily_ann", label: "Sharpe", hint: "연율, 0 기준", fmt: v => num(v), ax: v => num(v, 0), ref: 0, better: 1 },
  { k: "total_return_pp", label: "누적 순수익", hint: "거래당 1단위 합", fmt: v => pp(v), ax: v => num(v, 0), ref: 0, better: 1 },
  { k: "trades", label: "거래 수", hint: "표본이 클수록 신뢰", fmt: v => String(v), ax: v => String(Math.round(v)), better: 0 },
];

function smallMultiples(sel) {
  const W = 300, RH = 24, top = 8, bot = 22, L = 24, R = 78;
  return METRICS.map(M => {
    const v = r => r.metrics[M.k], bv = baselines().map(v).filter(x => x != null);
    let [lo, hi] = extent([...sel.flatMap(r => [v(r), ...(M.ci ? M.ci(r.metrics) : [])]), ...bv, ...(M.ref != null ? [M.ref] : [])]);
    if (["trades", "accuracy", "win_rate"].includes(M.k)) lo = Math.max(0, lo);
    const H = top + sel.length * RH + bot, x = scale(lo, hi, L, W - R);
    const vals = sel.map(v).filter(x => x != null), best = M.better && sel.length > 1 ? Math.max(...vals) : null;
    return `<div class="mcell"><h3>${M.label}</h3><div class="hint">${M.hint}</div>
      <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${M.label} 비교">
      ${bv.length ? `<rect x="${x(Math.min(...bv))}" y="${top - 4}" width="${Math.max(2, x(Math.max(...bv)) - x(Math.min(...bv)))}" height="${sel.length * RH + 4}" rx="3" fill="var(--band)" opacity=".55"/>` : ""}
      <g class="axis">${ticks(lo, hi, 3).map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="${top - 4}" y2="${H - bot + 2}"/><text x="${x(t)}" y="${H - 6}" text-anchor="middle">${M.ax(t)}</text>`).join("")}</g>
      ${M.ref != null ? `<line x1="${x(M.ref)}" x2="${x(M.ref)}" y1="${top - 4}" y2="${H - bot + 2}" stroke="var(--zero)" stroke-width="1.5"/>` : ""}
      ${sel.map((r, i) => {
        const cy = top + i * RH + RH / 2 - 2, c = colorOf(r.run.run_id), val = v(r), ci = M.ci ? M.ci(r.metrics) : null;
        const tip = `${r.run.run_id}|${M.k}`;
        return `<g data-tip="${esc(tip)}"><rect x="0" y="${cy - RH / 2}" width="${W}" height="${RH}" fill="transparent"/>
          <text x="6" y="${cy + 4}" style="fill:var(--ink-2);font-weight:700">${S.selected.get(r.run.run_id) + 1}</text>
          ${ci && ci[0] != null ? `<line x1="${x(ci[0])}" x2="${x(ci[1])}" y1="${cy}" y2="${cy}" stroke="${c}" stroke-width="2" stroke-linecap="round"/>` : ""}
          ${val != null ? `<circle cx="${x(val)}" cy="${cy}" r="5.5" fill="${c}" stroke="var(--panel)" stroke-width="2"/>` : ""}
          <text x="${W - R + 8}" y="${cy + 4}" style="fill:var(--ink)">${M.fmt(val)}${best != null && val === best ? " ▲" : ""}</text></g>`;
      }).join("")}</svg></div>`;
  }).join("");
}

function curveChart(sel) {
  const base = baselines().filter(b => !S.selected.has(b.run.run_id)), all = [...base, ...sel];
  const W = 640, H = 300, P = { l: 44, r: 12, t: 10, b: 26 };
  const days = all[0].metrics.curve.map(p => p[0]), idx = Object.fromEntries(days.map((d, i) => [d, i]));
  const vals = all.flatMap(r => r.metrics.curve.map(p => p[1]));
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const x = scale(0, days.length - 1, P.l, W - P.r), y = scale(lo, hi, H - P.b, P.t);
  const path = r => r.metrics.curve.map((p, i) => `${i ? "L" : "M"}${x(idx[p[0]] ?? i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const months = days.map((d, i) => [d, i]).filter(([d], i) => i === 0 || d.slice(5, 7) !== days[i - 1].slice(5, 7));
  const stride = Math.ceil(months.length / 7);
  return `<div class="legend">${sel.map(r => `<span><span class="line" style="border-color:${colorOf(r.run.run_id)}"></span>${S.selected.get(r.run.run_id) + 1}. ${esc(r.run.run_id)}</span>`).join("")}
      ${base.length ? `<span><span class="line" style="border-color:var(--band);border-top-style:dashed"></span>무작위 기준선 ${base.length}개</span>` : ""}</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="누적 순수익률 비교" id="curveSvg">
      <g class="axis">${ticks(lo, hi).map(v => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${P.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`).join("")}</g>
      <line x1="${P.l}" x2="${W - P.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--zero)"/>
      ${months.filter((_, i) => i % stride === 0).map(([d, i]) => `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${d.slice(2, 7).replace("-", ".")}</text>`).join("")}
      ${base.map(r => `<path d="${path(r)}" fill="none" stroke="var(--band)" stroke-width="1.5" stroke-dasharray="4 3"/>`).join("")}
      ${sel.map(r => `<path d="${path(r)}" fill="none" stroke="${colorOf(r.run.run_id)}" stroke-width="2" stroke-linejoin="round"/>`).join("")}
      <line id="xh" y1="${P.t}" y2="${H - P.b}" stroke="var(--ink-3)" visibility="hidden"/><g id="dots"></g>
      <rect id="hit" x="${P.l}" y="${P.t}" width="${W - P.l - P.r}" height="${H - P.t - P.b}" fill="transparent"/>
    </svg>`;
}
function bindCurve(sel) {
  const svg = $("#curveSvg"); if (!svg) return;
  const base = baselines().filter(b => !S.selected.has(b.run.run_id)), all = [...base, ...sel];
  const W = 640, H = 300, P = { l: 44, r: 12, t: 10, b: 26 };
  const days = all[0].metrics.curve.map(p => p[0]);
  const vals = all.flatMap(r => r.metrics.curve.map(p => p[1]));
  const x = scale(0, days.length - 1, P.l, W - P.r), y = scale(Math.min(0, ...vals), Math.max(0, ...vals), H - P.b, P.t);
  const hit = $("#hit", svg);
  hit.addEventListener("mousemove", e => {
    const b = svg.getBoundingClientRect(), sx = (e.clientX - b.left) * W / b.width;
    const i = Math.max(0, Math.min(days.length - 1, Math.round((sx - P.l) / ((W - P.l - P.r) / (days.length - 1)))));
    $("#xh", svg).setAttribute("x1", x(i)); $("#xh", svg).setAttribute("x2", x(i)); $("#xh", svg).setAttribute("visibility", "visible");
    const pts = sel.map(r => [r, r.metrics.curve.find(p => p[0] === days[i])?.[1]]);
    $("#dots", svg).innerHTML = pts.filter(p => p[1] != null).map(([r, v]) => `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="${colorOf(r.run.run_id)}" stroke="var(--panel)" stroke-width="2"/>`).join("");
    showTip(`<div class="t">${days[i]}</div>` + pts.sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0)).map(([r, v]) =>
      `<div class="row"><span>${S.selected.get(r.run.run_id) + 1}. ${esc(r.run.run_id)}</span><b>${pp(v)}</b></div>`).join(""), e);
  });
  hit.addEventListener("mouseleave", () => { hideTip(); $("#xh", svg).setAttribute("visibility", "hidden"); $("#dots", svg).innerHTML = ""; });
}

function scatter() {
  const MX = METRICS.find(m => m.k === S.sx), MY = METRICS.find(m => m.k === S.sy);
  const pts = inSplit().filter(r => r.metrics[MX.k] != null && r.metrics[MY.k] != null);
  if (!pts.length) return `<div class="empty">표시할 run이 없습니다.</div>`;
  const W = 520, H = 340, p = { l: 52, r: 16, t: 18, b: 36 };
  const [x0, x1] = extent(pts.map(r => r.metrics[MX.k]), 0.08), [y0, y1] = extent(pts.map(r => r.metrics[MY.k]), 0.08);
  const x = scale(x0, x1, p.l, W - p.r), y = scale(y0, y1, H - p.b, p.t);
  const order = [...pts].sort((a, b) => (S.selected.has(a.run.run_id) ? 1 : 0) - (S.selected.has(b.run.run_id) ? 1 : 0));
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="모델 지도">
    <g class="axis">${ticks(x0, x1, 5).map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="${p.t}" y2="${H - p.b}"/><text x="${x(t)}" y="${H - p.b + 14}" text-anchor="middle">${MX.ax(t)}</text>`).join("")}
    ${ticks(y0, y1, 5).map(t => `<line x1="${p.l}" x2="${W - p.r}" y1="${y(t)}" y2="${y(t)}"/><text x="${p.l - 6}" y="${y(t) + 4}" text-anchor="end">${MY.ax(t)}</text>`).join("")}</g>
    ${MX.ref != null && MX.ref > x0 && MX.ref < x1 ? `<line x1="${x(MX.ref)}" x2="${x(MX.ref)}" y1="${p.t}" y2="${H - p.b}" stroke="var(--zero)"/>` : ""}
    ${MY.ref != null && MY.ref > y0 && MY.ref < y1 ? `<line x1="${p.l}" x2="${W - p.r}" y1="${y(MY.ref)}" y2="${y(MY.ref)}" stroke="var(--zero)"/>` : ""}
    <text x="${W - p.r}" y="${H - 6}" text-anchor="end" style="fill:var(--ink-2)">${MX.label}</text>
    <text x="${p.l}" y="${p.t - 6}" style="fill:var(--ink-2)">${MY.label}</text>
    ${order.map(r => {
      const id = r.run.run_id, c = colorOf(id), cx = x(r.metrics[MX.k]), cy = y(r.metrics[MY.k]), isB = r.run.model.family === "baseline";
      const mark = c ? `<circle cx="${cx}" cy="${cy}" r="6.5" fill="${c}" stroke="var(--panel)" stroke-width="2"/><text x="${cx + 9}" y="${cy + 4}" style="fill:var(--ink);font-weight:700">${S.selected.get(id) + 1}</text>`
        : isB ? `<circle cx="${cx}" cy="${cy}" r="4.5" fill="none" stroke="var(--ink-3)" stroke-width="1.5"/>`
        : `<circle cx="${cx}" cy="${cy}" r="4.5" fill="var(--ink-3)" opacity=".5"/>`;
      return `<g class="pt" data-id="${esc(id)}" data-tip="${esc(id)}" style="cursor:pointer"><circle cx="${cx}" cy="${cy}" r="11" fill="transparent"/>${mark}</g>`;
    }).join("")}</svg>`;
}

function renderCompare() {
  const el = $("#compareView"), sel = selectedRuns();
  $("#compareCount").textContent = sel.length ? String(sel.length) : "";
  if (S.view !== "compare") { el.hidden = true; return; }
  el.hidden = false;
  if (!sel.length) {
    el.innerHTML = `<div class="panel"><div class="empty"><b>비교할 run을 고르세요</b>순위표에서 체크하거나 아래 모델 지도에서 점을 누르면 비교 목록에 추가됩니다.</div></div>
      <div class="section" style="margin-top:16px">${scatterSection()}</div>`;
    bindScatter(); return;
  }
  el.innerHTML = `<div class="compare-head">${sel.map(r => `<span class="run">${swatch(r.run.run_id)}${esc(r.run.run_id)} ${famTag(r)}</span>`).join("")}
      <button class="btn small ghost" id="backToBoard">순위로 돌아가기</button></div>
    <section class="section"><h2>지표별 비교</h2><p>회색 띠는 같은 구간 무작위 기준선의 최소~최대, 세로선은 기준값, 가는 선은 95% 구간입니다. ▲는 고른 run 중 가장 좋은 값입니다.</p>
      <div class="mx">${smallMultiples(sel)}</div></section>
    <div class="two">
      <section class="panel chart-panel"><h2 style="font-size:15px;margin:0 0 4px">누적 순수익률</h2>
        <p class="note" style="color:var(--ink-2);font-size:13px;margin:0 0 8px">거래당 1단위를 청산일에 더한 값(%p)입니다. 계좌 수익률이 아니라 신호 품질 비교용입니다.</p>${curveChart(sel)}</section>
      <section class="panel chart-panel">${scatterSection()}</section>
    </div>`;
  bindTips($(".mx", el), t => {
    const [id, k] = t.split("|"), r = byId(id), M = METRICS.find(m => m.k === k), ci = M.ci ? M.ci(r.metrics) : null;
    const bv = baselines().map(b => b.metrics[k]).filter(x => x != null);
    return `<div class="t">${esc(id)}</div><div class="row"><span>${M.label}</span><b>${M.fmt(r.metrics[k])}</b></div>`
      + (ci ? `<div class="row"><span>95% 구간</span><span>${M.fmt(ci[0])} … ${M.fmt(ci[1])}</span></div>` : "")
      + (bv.length ? `<div class="row"><span>기준선 범위</span><span>${M.fmt(Math.min(...bv))} … ${M.fmt(Math.max(...bv))}</span></div>` : "");
  });
  bindCurve(sel); bindScatter();
}
function scatterSection() {
  const opts = sel => METRICS.map(m => `<option value="${m.k}" ${m.k === sel ? "selected" : ""}>${m.label}</option>`).join("");
  return `<h2 style="font-size:15px;margin:0 0 4px">모델 지도</h2>
    <p class="note" style="color:var(--ink-2);font-size:13px;margin:0 0 8px">이 구간의 모든 run을 두 지표로 배치합니다. 점을 누르면 비교 목록에 추가하거나 뺍니다. 빈 원은 무작위 기준선입니다.</p>
    <div class="controls"><label class="field">가로 <select class="select" id="sx">${opts(S.sx)}</select></label>
    <label class="field">세로 <select class="select" id="sy">${opts(S.sy)}</select></label></div>
    <div id="scatter">${scatter()}</div>`;
}
function bindScatter() {
  const root = $("#scatter"); if (!root) return;
  const MX = METRICS.find(m => m.k === S.sx), MY = METRICS.find(m => m.k === S.sy);
  bindTips(root, id => { const r = byId(id); return `<div class="t">${esc(id)}</div><div class="row"><span>${MX.label}</span><span>${MX.fmt(r.metrics[MX.k])}</span></div><div class="row"><span>${MY.label}</span><span>${MY.fmt(r.metrics[MY.k])}</span></div>`; });
  $$(".pt", root).forEach(g => g.addEventListener("click", () => { hideTip(); toggleSelect(g.dataset.id); }));
}

// ---------- tray ----------
function renderTray() {
  const sel = selectedRuns(), tray = $("#tray");
  tray.hidden = !sel.length || S.view !== "board";
  $("#trayChips").innerHTML = sel.map(r => `<span class="chip">${swatch(r.run.run_id)}${esc(r.run.run_id)}<button data-remove="${esc(r.run.run_id)}" aria-label="${esc(r.run.run_id)} 비교에서 빼기">✕</button></span>`).join("");
}

// ---------- orchestration ----------
function renderAll() {
  $("#trackName").textContent = D.tracks[S.track].title;
  $("#view-board").setAttribute("aria-selected", S.view === "board");
  $("#view-compare").setAttribute("aria-selected", S.view === "compare");
  $("#boardView").hidden = S.view !== "board";
  renderSummary(); renderBoard(); renderInspector(); renderCompare(); renderTray(); writeUrl();
}

function bind() {
  document.addEventListener("click", e => {
    const t = e.target.closest("button, input[type=checkbox], tr[data-id]");
    if (!t) return;
    if (t.dataset.copy) return copy(t.dataset.copy);
    if (t.dataset.sort) { S.sort = { key: t.dataset.sort, dir: S.sort.key === t.dataset.sort ? -S.sort.dir : -1 }; return renderBoard(); }
    if (t.dataset.split) { S.split = t.dataset.split; S.active = null; defaultPick(); return renderAll(); }
    if (t.dataset.fam) { S.fams.has(t.dataset.fam) ? S.fams.delete(t.dataset.fam) : S.fams.add(t.dataset.fam); return renderAll(); }
    if (t.dataset.tab) { S.tab = t.dataset.tab; return renderInspector(); }
    if (t.dataset.remove) return toggleSelect(t.dataset.remove);
    if (t.id === "gatesOnly") { S.gatesOnly = !S.gatesOnly; return renderAll(); }
    if (t.id === "closeInspector") { S.active = null; return renderAll(); }
    if (t.id === "inspectorSelect") return toggleSelect(S.active);
    if (t.id === "trayOpen" || t.id === "view-compare") { S.view = "compare"; return renderAll(); }
    if (t.id === "backToBoard" || t.id === "view-board") { S.view = "board"; return renderAll(); }
    if (t.id === "trayClear") { S.selected.clear(); return renderAll(); }
    if (t.id === "theme") {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next; localStorage.setItem("arena-theme", next); return renderAll();
    }
    const row = t.closest("tr[data-id]");
    if (row && t.type === "checkbox") return toggleSelect(row.dataset.id);
    if (row) { S.active = S.active === row.dataset.id ? null : row.dataset.id; return renderAll(); }
  });
  document.addEventListener("keydown", e => {
    const row = e.target.closest && e.target.closest("tr[data-id]");
    if (row && e.target === row && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      if (e.key === " ") toggleSelect(row.dataset.id); else { S.active = row.dataset.id; renderAll(); }
      $(`tr[data-id="${CSS.escape(row.dataset.id)}"]`)?.focus();
    }
    if (e.key === "Escape" && S.active) { S.active = null; renderAll(); }
  });
  document.addEventListener("change", e => {
    if (e.target.id === "sx" || e.target.id === "sy") { S[e.target.id] = e.target.value; $("#scatter").innerHTML = scatter(); bindScatter(); }
    if (e.target.id === "track") { S.track = e.target.value; S.split = Object.keys(D.tracks[S.track].windows)[0]; S.active = null; defaultPick(); renderAll(); }
  });
  $("#q").addEventListener("input", e => { S.q = e.target.value; renderBoard(); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => {
    if (!localStorage.getItem("arena-theme")) { document.documentElement.dataset.theme = e.matches ? "dark" : "light"; renderAll(); }
  });
}

async function main() {
  if (location.protocol === "https:") $("#logout").hidden = false;
  let res;
  try {
    res = await fetch("data.json", { cache: "no-store" });
    if (res.status === 401) { location.reload(); return; } // session expired: reload goes through login
    D = await res.json();
  } catch (err) {
    $("#loadError").hidden = false;
    $("#loadError").textContent = `대시보드 데이터를 불러오지 못했습니다 (${err.message}). 로컬에서는 'uv run python -m arena build' 후 'serve'로 여세요.`;
    $("#headline").textContent = "데이터 없음";
    return;
  }
  $("#track").innerHTML = Object.entries(D.tracks).map(([k, t]) => `<option value="${k}">${esc(t.title)}</option>`).join("");
  S.track = Object.keys(D.tracks)[0];
  const u = readUrl(), windows = Object.keys(D.tracks[S.track].windows);
  const count = s => D.runs.filter(r => r.run.track_id === S.track && r.run.split === s).length;
  S.split = windows.includes(u.split) ? u.split : windows.includes("dev-late") && count("dev-late") ? "dev-late" : windows[0];
  if (u.cmp) u.cmp.split(",").filter(byId).slice(0, 4).forEach((id, i) => S.selected.set(id, i)); else defaultPick();
  S.active = u.run && byId(u.run) ? u.run : null;
  S.view = u.view === "compare" ? "compare" : "board";
  bind(); renderAll();
}
main();
