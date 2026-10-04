"use strict";

/* 口述史发布许可校验台 —— 前端交互（原生 JavaScript） */

const MEDIA = [
  { value: "audio", label: "音频" },
  { value: "text", label: "文字" },
];

const state = {
  interviews: [],
  currentId: null,
  detail: null, // {id, title, duration_seconds, speakers, segments, licenses}
  remBatch: [], // 补件批次草稿 [{speaker_id, start, end, allow, deny, embargo, note}]
  remResult: null, // 最近一次补件试算 / 写入结果
  credentials: [], // 发行凭据列表（含当前状态）
  credentialDetail: null, // 正在查阅的凭据复核视图
  placements: [], // 渠道投放台账列表（含投放状态与凭据风险）
  placementDetail: null, // 正在查阅的投放记录详情
  placementCredentialId: null, // 登记投放时选中的凭据
};

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmt(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

function parseTimecode(str) {
  str = String(str ?? "").trim();
  if (!str) return NaN;
  const parts = str.split(":");
  if (parts.length > 3) return NaN;
  let sec = 0;
  for (const p of parts) {
    if (!/^\d+$/.test(p)) return NaN;
    sec = sec * 60 + parseInt(p, 10);
  }
  return sec;
}

function setStatus(msg, isError = false) {
  const el = $("status-line");
  el.textContent = msg || "";
  el.className = isError ? "error" : "";
}

async function api(path, method = "GET", body) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error("无法连接服务器");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
  return data;
}

/* 包装异步操作，统一错误提示 */
async function run(fn) {
  try {
    await fn();
    setStatus("");
  } catch (e) {
    setStatus(e.message, true);
  }
}

/* ---------- 数据加载 ---------- */

async function loadInterviews() {
  state.interviews = await api("/api/interviews");
  if (!state.interviews.some((i) => i.id === state.currentId)) {
    state.currentId = state.interviews.length ? state.interviews[0].id : null;
  }
  await refreshDetail();
  render();
}

async function refreshDetail() {
  state.detail = state.currentId
    ? await api(`/api/interviews/${state.currentId}`)
    : null;
  // 资料变化会影响凭据状态（待复核 / 来源缺失）与投放风险，随访谈详情一并刷新
  state.credentials = await api("/api/credentials");
  state.placements = await api("/api/placements");
}

async function loadCredentials() {
  state.credentials = await api("/api/credentials");
  renderCredentials();
}

async function loadPlacements() {
  state.placements = await api("/api/placements");
  renderPlacements();
}

/* ---------- 渲染 ---------- */

function render() {
  const sel = $("interview-select");
  sel.innerHTML = state.interviews.length
    ? state.interviews.map((i) =>
        `<option value="${i.id}" ${i.id === state.currentId ? "selected" : ""}>`
        + `#${i.id} ${esc(i.title)}（${fmt(i.duration_seconds)}）</option>`).join("")
    : `<option value="">（暂无访谈，请先新建）</option>`;

  renderCredentials(); // 凭据列表与访谈选择无关，始终渲染
  renderPlacements(); // 投放台账与访谈选择无关，始终渲染

  const has = !!state.detail;
  $("detail-panels").classList.toggle("hidden", !has);
  $("interview-edit").classList.toggle("hidden", !has);
  if (!has) return;

  $("edit-title").value = state.detail.title;
  $("edit-duration").value = fmt(state.detail.duration_seconds);
  renderSpeakers();
  renderSegments();
  renderLicenses();
  renderRemediate();
  renderRemResult();
}

function speakerOptions(selected) {
  return state.detail.speakers.map((s) =>
    `<option value="${s.id}" ${s.id === selected ? "selected" : ""}>${esc(s.name)}</option>`
  ).join("");
}

function speakerName(id) {
  const sp = state.detail.speakers.find((s) => s.id === id);
  return sp ? sp.name : `#${id}`;
}

function renderSpeakers() {
  const rows = state.detail.speakers.map((s) => `
    <tr>
      <td>#${s.id}</td>
      <td><input id="sp-name-${s.id}" value="${esc(s.name)}"></td>
      <td>
        <button onclick="run(() => saveSpeaker(${s.id}))">保存</button>
        <button class="danger" onclick="run(() => delSpeaker(${s.id}))">删除</button>
      </td>
    </tr>`).join("");
  $("speakers-body").innerHTML =
    rows || `<tr><td colspan="3" class="empty">暂无说话人</td></tr>`;
}

function renderSegments() {
  const rows = state.detail.segments.map((g) => `
    <tr>
      <td>#${g.id}</td>
      <td><select id="seg-${g.id}-sp">${speakerOptions(g.speaker_id)}</select></td>
      <td><input id="seg-${g.id}-start" class="tc-input" value="${fmt(g.start_seconds)}"></td>
      <td><input id="seg-${g.id}-end" class="tc-input" value="${fmt(g.end_seconds)}"></td>
      <td>
        <button onclick="run(() => saveSegment(${g.id}))">保存</button>
        <button class="danger" onclick="run(() => delSegment(${g.id}))">删除</button>
      </td>
    </tr>`).join("");
  $("segments-body").innerHTML =
    rows || `<tr><td colspan="5" class="empty">暂无发言区间</td></tr>`;
  $("new-seg-speaker").innerHTML = speakerOptions(null);
}

function mediaCheckboxes(prefix, picked) {
  return MEDIA.map((m) =>
    `<label><input type="checkbox" id="${prefix}-${m.value}"`
    + `${picked.includes(m.value) ? " checked" : ""}>${m.label}</label>`
  ).join("");
}

function readMediaCheckboxes(prefix) {
  return MEDIA.filter((m) => $(`${prefix}-${m.value}`).checked)
    .map((m) => m.value);
}

function renderLicenses() {
  const rows = state.detail.licenses.map((l) => `
    <tr>
      <td>#${l.id}</td>
      <td>${esc(speakerName(l.speaker_id))}</td>
      <td><input id="lic-${l.id}-start" class="tc-input" value="${fmt(l.start_seconds)}"></td>
      <td><input id="lic-${l.id}-end" class="tc-input" value="${fmt(l.end_seconds)}"></td>
      <td>${mediaCheckboxes(`lic-${l.id}-allow`, l.allow_media)}</td>
      <td>${mediaCheckboxes(`lic-${l.id}-deny`, l.deny_media)}</td>
      <td><input type="date" id="lic-${l.id}-emb" value="${esc(l.embargo_until || "")}"></td>
      <td><input id="lic-${l.id}-note" value="${esc(l.note)}"></td>
      <td>
        <button onclick="run(() => saveLicense(${l.id}))">保存</button>
        <button class="danger" onclick="run(() => delLicense(${l.id}))">删除</button>
      </td>
    </tr>`).join("");
  $("licenses-body").innerHTML =
    rows || `<tr><td colspan="9" class="empty">暂无授权记录</td></tr>`;
  $("new-lic-speaker").innerHTML = speakerOptions(null);
}

function clearResults() {
  $("results-summary").textContent = "尚未校验。";
  $("timeline").innerHTML = "";
  $("results-body").innerHTML = "";
}

function intervalRowsHtml(intervals) {
  return intervals.map((it) => `
    <tr class="${it.status}">
      <td class="tc">${it.start_timecode} – ${it.end_timecode}</td>
      <td>${fmt(it.end_seconds - it.start_seconds)}</td>
      <td><span class="badge ${it.status}">${it.status_label}</span></td>
      <td>${it.speakers.length ? esc(it.speakers.join("、")) : "—"}</td>
      <td>${it.reasons.length
        ? it.reasons.map(esc).join("<br>")
        : (it.status === "no_speech" ? "无发言，无需授权" : "全部发言人授权有效")}</td>
    </tr>`).join("");
}

function renderResults(data) {
  const { intervals, summary } = data;
  const mediumLabel = data.medium === "audio" ? "音频" : "文字";
  $("results-summary").innerHTML =
    `发布日期 <b>${esc(data.publication_date)}</b> · 媒介 <b>${mediumLabel}</b> · `
    + `总时长 <b>${fmt(summary.duration_seconds)}</b>，`
    + `可发布 <b class="ok-text">${fmt(summary.publishable_seconds)}</b>，`
    + `不可发布 <b class="bad-text">${fmt(summary.blocked_seconds)}</b>`;

  $("timeline").innerHTML = intervals.map((it) => {
    const width = (it.end_seconds - it.start_seconds)
      / summary.duration_seconds * 100;
    const tip = `${it.start_timecode}–${it.end_timecode} ${it.status_label}`
      + (it.reasons.length ? "\n" + it.reasons.join("\n") : "");
    return `<div class="seg ${it.status}" style="width:${width}%"`
      + ` title="${esc(tip)}"></div>`;
  }).join("");

  $("results-body").innerHTML = intervalRowsHtml(intervals);
}

/* ---------- 访谈操作 ---------- */

async function createInterview() {
  const title = $("new-title").value.trim();
  const duration_seconds = parseTimecode($("new-duration").value);
  if (!title) throw new Error("请填写访谈标题");
  if (!Number.isInteger(duration_seconds) || duration_seconds <= 0) {
    throw new Error("时长格式应为 HH:MM:SS");
  }
  const created = await api("/api/interviews", "POST", { title, duration_seconds });
  state.currentId = created.id;
  $("new-title").value = "";
  await loadInterviews();
}

async function saveInterview() {
  const title = $("edit-title").value.trim();
  const duration_seconds = parseTimecode($("edit-duration").value);
  if (!title) throw new Error("请填写访谈标题");
  if (!Number.isInteger(duration_seconds) || duration_seconds <= 0) {
    throw new Error("时长格式应为 HH:MM:SS");
  }
  await api(`/api/interviews/${state.currentId}`, "PUT", { title, duration_seconds });
  await loadInterviews();
}

async function deleteInterview() {
  if (!confirm("确定删除该访谈及其全部说话人、发言与授权记录？")) return;
  await api(`/api/interviews/${state.currentId}`, "DELETE");
  state.currentId = null;
  state.remBatch = [];
  state.remResult = null;
  clearResults();
  await loadInterviews();
}

/* ---------- 说话人操作 ---------- */

async function addSpeaker() {
  const name = $("new-speaker-name").value.trim();
  if (!name) throw new Error("请填写说话人姓名");
  await api(`/api/interviews/${state.currentId}/speakers`, "POST", { name });
  $("new-speaker-name").value = "";
  await refreshDetail();
  render();
}

async function saveSpeaker(id) {
  const name = $(`sp-name-${id}`).value.trim();
  if (!name) throw new Error("说话人姓名不能为空");
  await api(`/api/speakers/${id}`, "PUT", { name });
  await refreshDetail();
  render();
}

async function delSpeaker(id) {
  if (!confirm("删除该说话人及其发言、授权记录？")) return;
  await api(`/api/speakers/${id}`, "DELETE");
  await refreshDetail();
  render();
}

/* ---------- 发言区间操作 ---------- */

function readSegmentInputs(prefix) {
  const start_seconds = parseTimecode($(`${prefix}-start`).value);
  const end_seconds = parseTimecode($(`${prefix}-end`).value);
  if (!Number.isInteger(start_seconds) || !Number.isInteger(end_seconds)) {
    throw new Error("时间码格式应为 HH:MM:SS");
  }
  if (end_seconds <= start_seconds) throw new Error("结束时间必须大于开始时间");
  return { start_seconds, end_seconds };
}

async function addSegment() {
  const speaker_id = Number($("new-seg-speaker").value);
  if (!speaker_id) throw new Error("请先添加说话人");
  const range = readSegmentInputs("new-seg");
  await api(`/api/interviews/${state.currentId}/segments`, "POST",
    { speaker_id, ...range });
  await refreshDetail();
  render();
}

async function saveSegment(id) {
  const speaker_id = Number($(`seg-${id}-sp`).value);
  const range = readSegmentInputs(`seg-${id}`);
  await api(`/api/segments/${id}`, "PUT", { speaker_id, ...range });
  await refreshDetail();
  render();
}

async function delSegment(id) {
  await api(`/api/segments/${id}`, "DELETE");
  await refreshDetail();
  render();
}

/* ---------- 授权区间操作 ---------- */

function readLicenseInputs(prefix) {
  const range = readSegmentInputs(prefix);
  return {
    ...range,
    allow_media: readMediaCheckboxes(`${prefix}-allow`),
    deny_media: readMediaCheckboxes(`${prefix}-deny`),
    embargo_until: $(`${prefix}-emb`).value || null,
    note: $(`${prefix}-note`).value.trim(),
  };
}

async function addLicense() {
  const speaker_id = Number($("new-lic-speaker").value);
  if (!speaker_id) throw new Error("请先添加说话人");
  const values = readLicenseInputs("new-lic");
  await api(`/api/speakers/${speaker_id}/licenses`, "POST", values);
  await refreshDetail();
  render();
}

async function saveLicense(id) {
  const values = readLicenseInputs(`lic-${id}`);
  await api(`/api/licenses/${id}`, "PUT", values);
  await refreshDetail();
  render();
}

async function delLicense(id) {
  await api(`/api/licenses/${id}`, "DELETE");
  await refreshDetail();
  render();
}

/* ---------- 校验 ---------- */

async function evaluate() {
  const publication_date = $("pub-date").value;
  const medium = $("medium").value;
  if (!publication_date) throw new Error("请选择发布日期");
  const data = await api(`/api/interviews/${state.currentId}/evaluate`, "POST",
    { publication_date, medium });
  renderResults(data);
}

/* ---------- 补件并复核 ---------- */

function renderRemediate() {
  const rows = state.remBatch.map((row, i) => `
    <tr>
      <td>${i + 1}</td>
      <td><select id="rem-${i}-sp">${speakerOptions(row.speaker_id)}</select></td>
      <td><input id="rem-${i}-start" class="tc-input" placeholder="00:00:00" value="${esc(row.start)}"></td>
      <td><input id="rem-${i}-end" class="tc-input" placeholder="00:10:00" value="${esc(row.end)}"></td>
      <td>${mediaCheckboxes(`rem-${i}-allow`, row.allow)}</td>
      <td>${mediaCheckboxes(`rem-${i}-deny`, row.deny)}</td>
      <td><input type="date" id="rem-${i}-emb" value="${esc(row.embargo || "")}"></td>
      <td><input id="rem-${i}-note" value="${esc(row.note || "")}"></td>
      <td><button class="danger" onclick="run(() => removeRemRow(${i}))">删除</button></td>
    </tr>`).join("");
  $("remediate-body").innerHTML =
    rows || `<tr><td colspan="9" class="empty">暂无补件，点击「添加一行」</td></tr>`;
}

/* 重渲染前把页面上的编辑同步回 state.remBatch */
function syncRemBatch() {
  state.remBatch = state.remBatch.map((row, i) => {
    if (!$(`rem-${i}-sp`)) return row;
    return {
      speaker_id: Number($(`rem-${i}-sp`).value) || null,
      start: $(`rem-${i}-start`).value,
      end: $(`rem-${i}-end`).value,
      allow: readMediaCheckboxes(`rem-${i}-allow`),
      deny: readMediaCheckboxes(`rem-${i}-deny`),
      embargo: $(`rem-${i}-emb`).value,
      note: $(`rem-${i}-note`).value,
    };
  });
}

async function addRemRow() {
  syncRemBatch();
  state.remBatch.push({
    speaker_id: state.detail.speakers.length ? state.detail.speakers[0].id : null,
    start: "", end: "", allow: ["audio"], deny: [], embargo: "", note: "",
  });
  renderRemediate();
}

async function removeRemRow(i) {
  syncRemBatch();
  state.remBatch.splice(i, 1);
  renderRemediate();
}

function renderRemResult() {
  const data = state.remResult;
  const box = $("remediate-result");
  if (!data) {
    box.innerHTML = "";
    return;
  }
  const mediumLabel = data.medium === "audio" ? "音频" : "文字";
  let headline;
  if (data.committed) {
    headline = `<p class="ok-text">✔ 整条时间轴均可发布，已一次性写入 `
      + `${data.inserted_license_ids.length} 条补充授权`
      + `（许可 ${data.inserted_license_ids.map((id) => "#" + id).join("、")}），`
      + `以下为变更后的区间。</p>`;
  } else if (data.blocked && data.blocked.length) {
    headline = `<p class="bad-text">✘ 合并补件后仍有 ${data.blocked.length} 个`
      + `不可发布区间，未写入任何记录。</p>`;
  } else {
    headline = `<p class="ok-text">✔ 试算通过：合并补件后整条时间轴均可发布`
      + `（尚未写入，确认无误请点击「补件并写入」）。</p>`;
  }
  const sum = (s) =>
    `可发布 ${fmt(s.publishable_seconds)} / 共 ${fmt(s.duration_seconds)}`
    + `（阻断 ${fmt(s.blocked_seconds)}）`;
  box.innerHTML = `
    <div class="summary">发布日期 <b>${esc(data.publication_date)}</b> · 媒介 <b>${mediumLabel}</b></div>
    ${headline}
    <p class="hint">试算前：${sum(data.before.summary)}<br>`
    + `${data.committed ? "写入后" : "合并补件后"}：${sum(data.after.summary)}</p>
    <table>
      <thead>
        <tr><th>区间（时间码）</th><th>时长</th><th>结果</th><th>说话人</th><th>原因 / 说明</th></tr>
      </thead>
      <tbody>${intervalRowsHtml(data.after.intervals)}</tbody>
    </table>`;
}

async function submitRemediate(commit) {
  syncRemBatch();
  if (!state.remBatch.length) throw new Error("请先添加补件行");
  const publication_date = $("pub-date").value;
  const medium = $("medium").value;
  if (!publication_date) throw new Error("请选择发布日期");
  const supplements = state.remBatch.map((row, i) => {
    if (!row.speaker_id) throw new Error(`第 ${i + 1} 行请选择说话人`);
    const start_seconds = parseTimecode(row.start);
    const end_seconds = parseTimecode(row.end);
    if (!Number.isInteger(start_seconds) || !Number.isInteger(end_seconds)) {
      throw new Error(`第 ${i + 1} 行时间码格式应为 HH:MM:SS`);
    }
    return {
      speaker_id: row.speaker_id,
      start_seconds,
      end_seconds,
      allow_media: row.allow,
      deny_media: row.deny,
      embargo_until: row.embargo || null,
      note: String(row.note || "").trim(),
    };
  });
  let resp;
  try {
    resp = await fetch(`/api/interviews/${state.currentId}/remediate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ publication_date, medium, commit, supplements }),
    });
  } catch (e) {
    throw new Error("无法连接服务器");
  }
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 409) {
    // 仍有阻断区间：展示试算结果与原因，再提示未写入
    state.remResult = data;
    renderRemResult();
    throw new Error(data.error || "仍存在不可发布区间，未写入任何补件");
  }
  if (!resp.ok) throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
  state.remResult = data;
  if (data.committed) {
    state.remBatch = [];
    await refreshDetail();
    render();
  } else {
    renderRemResult();
  }
}

/* ---------- 发行凭据 ---------- */

function mediumLabel(value) {
  return value === "audio" ? "音频" : "文字";
}

function statusBadge(status, label) {
  return `<span class="badge ${status}">${esc(label)}</span>`;
}

function renderCredentials() {
  const rows = state.credentials.map((c) => `
    <tr>
      <td>#${c.id}</td>
      <td>${esc(c.interview_title)}</td>
      <td>${esc(c.publication_date)}</td>
      <td>${mediumLabel(c.medium)}</td>
      <td class="tc">${esc(c.created_at)}</td>
      <td>${statusBadge(c.status, c.status_label)}${c.changes.length
        ? `<br><span class="hint">变化：${esc(c.changes.join("、"))}</span>` : ""}</td>
      <td><button onclick="run(() => viewCredential(${c.id}))">查阅</button></td>
    </tr>`).join("");
  $("credentials-body").innerHTML =
    rows || `<tr><td colspan="7" class="empty">暂无发行凭据</td></tr>`;
}

function intervalsTableHtml(intervals) {
  return `
    <table>
      <thead>
        <tr><th>区间（时间码）</th><th>时长</th><th>结果</th><th>说话人</th><th>原因 / 说明</th></tr>
      </thead>
      <tbody>${intervalRowsHtml(intervals)}</tbody>
    </table>`;
}

/* 封存失败（409）：展示阻断区间，不落任何记录 */
function renderCredentialFailure(data) {
  $("credential-detail").innerHTML = `
    <p class="bad-text">✘ ${esc(data.error)}</p>
    <p class="hint">可发布 ${fmt(data.summary.publishable_seconds)} / 共 `
    + `${fmt(data.summary.duration_seconds)}（阻断 ${fmt(data.summary.blocked_seconds)}），`
    + `未保存任何凭据记录。</p>
    ${intervalsTableHtml(data.blocked)}`;
}

function renderCredentialDetail() {
  const d = state.credentialDetail;
  const box = $("credential-detail");
  if (!d) {
    box.innerHTML = "";
    return;
  }
  const sealed = d.sealed.result.summary;
  let html = `
    <h3>凭据 #${d.id} · ${esc(d.interview_title)}</h3>
    <div class="summary">
      发布日期 <b>${esc(d.publication_date)}</b> · 媒介 <b>${mediumLabel(d.medium)}</b>
      · 封存于 <b class="tc">${esc(d.created_at)}</b>
      · 当前状态 ${statusBadge(d.status, d.status_label)}${d.changes.length
        ? `（变化：${esc(d.changes.join("、"))}）` : ""}
    </div>
    <h4>发行时结论（封存，不可修改）</h4>
    <p class="hint">总时长 ${fmt(sealed.duration_seconds)}，`
    + `可发布 ${fmt(sealed.publishable_seconds)}，`
    + `阻断 ${fmt(sealed.blocked_seconds)}。</p>
    ${intervalsTableHtml(d.sealed.result.intervals)}`;
  if (d.status === "missing") {
    html += `<p class="bad-text">来源缺失：原访谈已删除，无法重新校验；`
      + `以上为该凭据封存时的发行结论，仍可查阅。</p>`;
  } else {
    html += `<h4>当前复核（一致读取快照中重新校验）</h4>`;
    if (d.changes.length) {
      html += `<p class="bad-text">时长 / 说话人 / 发言 / 授权有变化`
        + `（${esc(d.changes.join("、"))}），标记为「待复核」`
        + `${d.current.blocked.length ? "。" : "；当前重新校验仍可发布。"}</p>`;
    } else {
      html += `<p class="ok-text">✔ 现有资料与发行时一致。</p>`;
    }
    html += d.current.blocked.length
      ? `<p class="bad-text">当前存在 ${d.current.blocked.length} 个阻断区间：</p>`
        + intervalsTableHtml(d.current.blocked)
      : `<p class="hint">当前无阻断区间。</p>`;
  }
  box.innerHTML = html;
}

async function createCredential() {
  if (!state.currentId) throw new Error("请先选择访谈");
  const publication_date = $("pub-date").value;
  const medium = $("medium").value;
  if (!publication_date) throw new Error("请选择发布日期");
  let resp;
  try {
    resp = await fetch(`/api/interviews/${state.currentId}/credentials`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ publication_date, medium }),
    });
  } catch (e) {
    throw new Error("无法连接服务器");
  }
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 409) {
    // 仍有阻断区间：展示阻断明细，不落任何记录
    state.credentialDetail = null;
    renderCredentialFailure(data);
    throw new Error(data.error || "仍存在不可发布区间，未保存凭据");
  }
  if (!resp.ok) throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
  state.credentialDetail = null;
  $("credential-detail").innerHTML =
    `<p class="ok-text">✔ 整条时间轴均可发布，凭据 #${data.id} 已封存`
    + `（不可修改，可点击下方「查阅」复核）。</p>`;
  await loadCredentials();
}

async function viewCredential(id) {
  state.credentialDetail = await api(`/api/credentials/${id}`);
  renderCredentialDetail();
}

/* ---------- 渠道投放台账 ---------- */

function renderPlacements() {
  const sel = $("placement-credential");
  sel.innerHTML = state.credentials.length
    ? state.credentials.map((c) =>
        `<option value="${c.id}"${c.id === state.placementCredentialId ? " selected" : ""}`
        + `>#${c.id} ${esc(c.interview_title)}（${esc(c.publication_date)}`
        + ` · ${mediumLabel(c.medium)}）</option>`).join("")
    : `<option value="">（暂无发行凭据，请先封存）</option>`;
  const rows = state.placements.map((p) => `
    <tr>
      <td>#${p.id}</td>
      <td>#${p.credential_id}</td>
      <td>${esc(p.interview_title)}</td>
      <td>${esc(p.channel)}</td>
      <td class="url-cell"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a></td>
      <td class="tc">${esc(p.created_at)}</td>
      <td>${statusBadge(p.status, p.status_label)}${p.status === "withdrawn"
        ? `<br><span class="hint">撤下于 ${esc(p.withdrawn_at)}</span>` : ""}</td>
      <td>${statusBadge(p.risk, p.risk_label)}${p.changes.length
        ? `<br><span class="hint">变化：${esc(p.changes.join("、"))}</span>` : ""}</td>
      <td>
        <button onclick="run(() => viewPlacement(${p.id}))">查阅</button>
        ${p.status === "active"
          ? `<button class="danger" onclick="run(() => withdrawPlacement(${p.id}))">撤下</button>`
          : ""}
      </td>
    </tr>`).join("");
  $("placements-body").innerHTML =
    rows || `<tr><td colspan="9" class="empty">暂无投放记录</td></tr>`;
}

/* 登记失败（409）：说明原因，不落任何记录 */
function renderPlacementFailure(data) {
  let html = `<p class="bad-text">✘ ${esc(data.error)}</p>`
    + `<p class="hint">未保存任何投放记录。</p>`;
  if (data.blocked && data.blocked.length) {
    html += `<p class="bad-text">当前 ${data.blocked.length} 个不可发布区间：</p>`
      + intervalsTableHtml(data.blocked);
  }
  $("placement-detail").innerHTML = html;
}

function renderPlacementDetail() {
  const d = state.placementDetail;
  const box = $("placement-detail");
  if (!d) {
    box.innerHTML = "";
    return;
  }
  const c = d.credential;
  const sealed = c.sealed_result.summary;
  let html = `
    <h3>投放 #${d.id} · ${esc(d.channel)}</h3>
    <div class="summary">
      凭据 <b>#${d.credential_id}</b> · 访谈 <b>${esc(c.interview_title)}</b>
      · 发布日期 <b>${esc(c.publication_date)}</b> · 媒介 <b>${mediumLabel(c.medium)}</b><br>
      投放链接 <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a>
      · 登记于 <b class="tc">${esc(d.created_at)}</b>
      · 投放状态 ${statusBadge(d.status, d.status_label)}
      · 凭据风险 ${statusBadge(d.risk, d.risk_label)}${d.changes.length
        ? `（变化：${esc(d.changes.join("、"))}）` : ""}
    </div>`;
  if (d.status === "withdrawn") {
    html += `<p class="hint">已撤下于 <span class="tc">${esc(d.withdrawn_at)}</span>`
      + `${d.withdraw_reason ? `，原因：${esc(d.withdraw_reason)}` : ""}。</p>`;
  } else {
    html += `<div class="row">
      <button class="danger" onclick="run(() => withdrawPlacement(${d.id}))">确认内容已撤下，标记为已撤下</button>
    </div>`;
  }
  html += `<h4>封存结论（凭据发行时，不可修改）</h4>
    <p class="hint">总时长 ${fmt(sealed.duration_seconds)}，`
    + `可发布 ${fmt(sealed.publishable_seconds)}，`
    + `阻断 ${fmt(sealed.blocked_seconds)}。</p>`;
  if (d.risk === "missing") {
    html += `<p class="bad-text">来源缺失：原访谈已删除，无法重新校验；`
      + `历史链接、时间与封存结论仍保留。</p>`;
  } else {
    if (d.changes.length) {
      html += `<p class="bad-text">资料与封存时不一致`
        + `（${esc(d.changes.join("、"))}），需处理`
        + `${d.current.blocked.length ? "；当前存在不可发布区间。" : "；当前重新校验仍可发布。"}</p>`;
    } else {
      html += `<p class="ok-text">✔ 现有资料与封存时一致。</p>`;
    }
    html += d.current.blocked.length
      ? `<p class="bad-text">当前存在 ${d.current.blocked.length} 个阻断区间：</p>`
        + intervalsTableHtml(d.current.blocked)
      : `<p class="hint">当前无阻断区间。</p>`;
  }
  box.innerHTML = html;
}

async function createPlacement() {
  const credential_id = Number($("placement-credential").value);
  if (!credential_id) throw new Error("请先选择发行凭据");
  const channel = $("placement-channel").value.trim();
  const url = $("placement-url").value.trim();
  if (!channel) throw new Error("请填写渠道名");
  if (!url) throw new Error("请填写投放链接");
  let resp;
  try {
    resp = await fetch(`/api/credentials/${credential_id}/placements`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel, url }),
    });
  } catch (e) {
    throw new Error("无法连接服务器");
  }
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 409) {
    // 来源复核未通过：说明原因，不落任何记录
    state.placementDetail = null;
    renderPlacementFailure(data);
    throw new Error(data.error || "来源复核未通过，未保存投放记录");
  }
  if (!resp.ok) throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
  state.placementDetail = null;
  $("placement-channel").value = "";
  $("placement-url").value = "";
  $("placement-detail").innerHTML =
    `<p class="ok-text">✔ 投放记录 #${data.id} 已登记（投放中，`
    + `登记时间 <span class="tc">${esc(data.created_at)}</span>）。</p>`;
  await loadPlacements();
}

async function viewPlacement(id) {
  state.placementDetail = await api(`/api/placements/${id}`);
  renderPlacementDetail();
}

async function withdrawPlacement(id) {
  const reason = prompt("确认渠道内容已撤下？请填写撤下原因（可留空）：");
  if (reason === null) return; // 用户取消
  await api(`/api/placements/${id}/withdraw`, "POST", { reason: reason.trim() });
  await loadPlacements();
  if (state.placementDetail && state.placementDetail.id === id) {
    await viewPlacement(id); // 刷新详情：已撤下 + 撤下时间，风险提示仍可查
  }
}

/* ---------- 启动 ---------- */

window.addEventListener("DOMContentLoaded", () => {
  $("pub-date").value = new Date().toISOString().slice(0, 10);
  $("btn-create-interview").addEventListener("click", () => run(createInterview));
  $("btn-save-interview").addEventListener("click", () => run(saveInterview));
  $("btn-delete-interview").addEventListener("click", () => run(deleteInterview));
  $("btn-refresh").addEventListener("click", () => run(loadInterviews));
  $("btn-add-speaker").addEventListener("click", () => run(addSpeaker));
  $("btn-add-segment").addEventListener("click", () => run(addSegment));
  $("btn-add-license").addEventListener("click", () => run(addLicense));
  $("btn-evaluate").addEventListener("click", () => run(evaluate));
  $("btn-rem-add").addEventListener("click", () => run(addRemRow));
  $("btn-rem-dry").addEventListener("click", () => run(() => submitRemediate(false)));
  $("btn-rem-commit").addEventListener("click", () => run(() => submitRemediate(true)));
  $("btn-create-credential").addEventListener("click", () => run(createCredential));
  $("btn-refresh-credentials").addEventListener("click", () => run(loadCredentials));
  $("btn-create-placement").addEventListener("click", () => run(createPlacement));
  $("btn-refresh-placements").addEventListener("click", () => run(loadPlacements));
  $("placement-credential").addEventListener("change", (e) => {
    state.placementCredentialId = Number(e.target.value) || null;
  });
  $("interview-select").addEventListener("change", (e) => run(async () => {
    state.currentId = Number(e.target.value) || null;
    state.remBatch = [];
    state.remResult = null;
    await refreshDetail();
    render();
    clearResults();
  }));
  run(loadInterviews);
});
