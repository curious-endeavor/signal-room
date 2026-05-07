const resultsMount = document.querySelector(".ajax-results");
const pollTimers = new Map();

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatScore(value) {
  const score = Number(value || 0);
  return Number.isFinite(score) ? String(Math.round(score)) : "0";
}

function sourceCountsMarkup(sourceCounts) {
  if (!Array.isArray(sourceCounts) || !sourceCounts.length) return "";
  return `<div class="source-counts" aria-label="Results by source">${
    sourceCounts.map((row) => `<span>${escapeHtml(row.source)} ${escapeHtml(row.count)}</span>`).join("")
  }</div>`;
}

function workerWindowMarkup(run, events = []) {
  if (!Array.isArray(events) || !events.length) return "";
  const live = ["queued", "running"].includes(run.status || "") ? `<span class="worker-live-dot">Live</span>` : "";
  return `
    <section class="worker-window" aria-label="Worker activity">
      <div class="worker-window-top">
        <span>Worker activity</span>
        ${live}
      </div>
      <div class="worker-log">
        ${events.map((event) => `
          <div class="worker-event is-${escapeHtml(event.kind || "info")}">
            <span class="worker-prompt">&gt;</span>
            <span>${escapeHtml(event.message || "")}</span>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function setFormBusy(form, busy, label = "Search") {
  const submitButton = form.querySelector("button[type='submit']");
  if (!submitButton) return;
  if (!submitButton.dataset.idleLabel) submitButton.dataset.idleLabel = submitButton.textContent;
  submitButton.disabled = busy;
  submitButton.textContent = busy ? label : submitButton.dataset.idleLabel;
}

function formQuery(form) {
  const data = new FormData(form);
  return String(data.get("query") || "").trim();
}

function showSearchNotice(message, tone = "pending") {
  if (!resultsMount) return;
  resultsMount.hidden = false;
  resultsMount.innerHTML = `
    <section class="pending-state ${tone === "error" ? "is-error" : ""}">
      ${tone === "error" ? "" : loadingMarkup()}
      <p>${escapeHtml(message)}</p>
    </section>
  `;
  resultsMount.scrollIntoView({ block: "nearest" });
}

function loadingMarkup() {
  return `
    <div class="loading-row" aria-hidden="true">
      <span class="spinner"></span>
      <span class="loading-dots"><span></span><span></span><span></span></span>
    </div>
  `;
}

function dateGroupsFromItems(items) {
  const groups = [];
  const byLabel = new Map();
  for (const item of items) {
    const label = item.date_group || "Results";
    if (!byLabel.has(label)) {
      const group = { label, items: [] };
      byLabel.set(label, group);
      groups.push(group);
    }
    byLabel.get(label).items.push(item);
  }
  return groups;
}

function resultRow(item, run) {
  return `
    <article class="result-row" data-item-id="${escapeHtml(item.id)}">
      <div class="result-topline">
        <a class="result-title" href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>
        <span class="score">${formatScore(item.score)}</span>
      </div>
      <div class="source-line">
        <span>${escapeHtml(item.display_source || item.source || "Source")}</span>
        <span>${escapeHtml(item.display_date || item.date || "")}</span>
        <span>${escapeHtml(item.pillar || "Unsorted")}</span>
        ${item.traction_label ? `<span>${escapeHtml(item.traction_label)}</span>` : ""}
      </div>
      <p class="summary">${item.summary_html || escapeHtml(item.summary_text || item.summary || "")}</p>
      <p class="angle"><strong>CE angle</strong> ${escapeHtml(item.suggested_ce_angle || "")}</p>
      <div class="result-actions">
        <form class="js-action-form" action="/create-content" data-api-action="/api/create-content" method="post">
          <input type="hidden" name="run_id" value="${escapeHtml(run.id)}">
          <input type="hidden" name="item_id" value="${escapeHtml(item.id)}">
          <button type="submit">Create Content</button>
        </form>
        <form class="js-action-form" action="/feedback" data-api-action="/api/feedback" method="post">
          <input type="hidden" name="run_id" value="${escapeHtml(run.id)}">
          <input type="hidden" name="item_id" value="${escapeHtml(item.id)}">
          <button class="icon-button" type="submit" aria-label="Thumbs down">Thumbs down</button>
        </form>
      </div>
    </article>
  `;
}

function renderResults(run, items, sourceCounts = [], dateGroups = [], workerEvents = []) {
  if (!resultsMount) return;
  resultsMount.hidden = false;
  resultsMount.dataset.runId = run.id || "";
  resultsMount.dataset.activeQuery = run.query || "";
  resultsMount.dataset.activeStatus = run.status || "";
  const status = ["queued", "running", "failed"].includes(run.status || "") ? `<span>${escapeHtml(run.status)}</span>` : "";
  const error = run.error ? `<p class="error-text">${escapeHtml(run.error)}</p>` : "";
  const includeSearch = resultsMount.dataset.includeSearch !== "false";
  const searchForm = includeSearch ? `
    <form class="search-form compact js-search-form" action="/search" method="post">
      <div class="search-box">
        <input name="query" type="search" value="${escapeHtml(run.query || "")}" aria-label="Search query">
        <button type="submit">Search</button>
      </div>
      <div class="search-meta">
        <label class="lookback-label">
          <span>Number of days looking back</span>
          <select name="lookback_days">
            ${[1, 7, 14, 30, 60, 90].map((days) => `<option value="${days}" ${Number(run.lookback_days) === days ? "selected" : ""}>${days}</option>`).join("")}
          </select>
        </label>
      </div>
    </form>
  ` : "";
  const pendingTitle = run.status === "queued" ? "Queued" : "Searching";
  const pendingMessage = run.status === "queued"
    ? "Waiting for the worker to pick this up."
    : "Fetching sources and scoring results. All-source searches can take several minutes.";
  const pending = ["queued", "running"].includes(run.status || "")
    ? `
      <section class="pending-state">
        ${loadingMarkup()}
        <div>
          <strong>${pendingTitle}</strong>
          <p>${pendingMessage}</p>
        </div>
      </section>
    `
    : "";
  const workerWindow = workerWindowMarkup(run, workerEvents);
  const groups = Array.isArray(dateGroups) && dateGroups.length ? dateGroups : dateGroupsFromItems(items);
  const rows = groups.map((group) => `
    <section class="date-group" aria-label="${escapeHtml(group.label)}">
      <h2 class="date-heading">${escapeHtml(group.label)}</h2>
      ${(group.items || group.rows || []).map((item) => resultRow(item, run)).join("")}
    </section>
  `).join("");

  resultsMount.innerHTML = `
    <section class="results-header">
      ${searchForm}
      <div class="run-meta">
        <span>${escapeHtml(run.lookback_days || 30)} days</span>
        <span>${escapeHtml(run.item_count || items.length)} results</span>
        ${status}
      </div>
      ${sourceCountsMarkup(sourceCounts)}
      ${error}
    </section>
    ${pending}
    ${workerWindow}
    <section class="results-list" aria-label="Search results">${rows}</section>
  `;
  scrollWorkerLogToBottom();
}

function scrollWorkerLogToBottom() {
  const log = resultsMount?.querySelector(".worker-log");
  if (!log) return;
  log.scrollTop = log.scrollHeight;
}

async function pollRun(runId) {
  if (!runId || runId === "sample" || pollTimers.has(runId)) return;
  let shouldContinue = true;
  let attempts = 0;
  const tick = async () => {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    const payload = await response.json();
    attempts += 1;
    if (payload.ok) {
      renderResults(payload.run, payload.items || [], payload.source_counts || [], payload.date_groups || [], payload.worker_events || []);
      shouldContinue = ["queued", "running"].includes(payload.run.status || "");
      if (shouldContinue && attempts >= 160) {
        shouldContinue = false;
        showSearchNotice("Still waiting. The worker may be stuck or one source may be timing out; refresh this run page in a moment.");
      }
      if (!shouldContinue) {
        clearInterval(pollTimers.get(runId));
        pollTimers.delete(runId);
      }
    }
  };
  await tick();
  if (shouldContinue && !pollTimers.has(runId)) {
    pollTimers.set(runId, setInterval(tick, 3000));
  }
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.matches(".js-search-form")) {
    event.preventDefault();
    const query = formQuery(form);
    if (!query) {
      showSearchNotice("Enter a search query first.", "error");
      return;
    }
    const activeStatus = resultsMount?.dataset.activeStatus || "";
    const activeQuery = (resultsMount?.dataset.activeQuery || "").trim();
    if (activeQuery === query && ["queued", "running"].includes(activeStatus)) {
      showSearchNotice(`That search is already ${activeStatus}.`);
      pollRun(resultsMount?.dataset.runId);
      return;
    }
    setFormBusy(form, true, "Searching...");
    showSearchNotice(`Starting search for "${query}"...`);
    try {
      const response = await fetch("/api/search", { method: "POST", body: new FormData(form) });
      if (!response.ok) throw new Error(`Search request failed (${response.status})`);
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Search failed");
      history.pushState({}, "", `/runs/${payload.run.id}`);
      renderResults(payload.run, payload.items || [], payload.source_counts || [], payload.date_groups || [], payload.worker_events || []);
      resultsMount?.scrollIntoView({ block: "nearest" });
      pollRun(payload.run.id);
    } catch (error) {
      showSearchNotice(error.message, "error");
    } finally {
      setFormBusy(form, false);
    }
    return;
  }

  if (form.matches(".js-action-form")) {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    try {
      await fetch(form.dataset.apiAction || form.action, { method: "POST", body: new FormData(form) });
    } finally {
      if (button) {
        button.textContent = "Saved";
        setTimeout(() => {
          button.disabled = false;
        }, 800);
      }
    }
  }
});

if (resultsMount?.dataset.runId) {
  pollRun(resultsMount.dataset.runId);
}
