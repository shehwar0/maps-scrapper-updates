const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const downloadBtn = document.getElementById("downloadBtn");
const statusEl = document.getElementById("status");
const bodyEl = document.getElementById("resultsBody");
const statsPanel = document.getElementById("statsPanel");
const modeDescription = document.getElementById("modeDescription");
const extractionModeSelect = document.getElementById("extractionMode");
const historyInfo = document.getElementById("historyInfo");
const historyCount = document.getElementById("historyCount");
const clearHistoryBtn = document.getElementById("clearHistoryBtn");
const historyFilesList = document.getElementById("historyFilesList");
const refreshHistoryFilesBtn = document.getElementById("refreshHistoryFilesBtn");
const selectAllHistoryFilesBtn = document.getElementById("selectAllHistoryFilesBtn");
const clearSelectedHistoryFilesBtn = document.getElementById("clearSelectedHistoryFilesBtn");
const locationSuggestions = document.getElementById("locationSuggestions");
const MAX_RESULTS_LIMIT = 500;
const LOCATION_SUGGESTION_LIMIT = 8;

let pollingId = null;
let outputHistoryFiles = [];
const selectedHistoryFiles = new Set();
let activeScrapeController = null;
let stopRequestedByUser = false;
let lastRenderedCount = 0;
let backendCooldownUntil = 0;
let locationSuggestController = null;
let locationSuggestTimer = null;
let currentTarget = 0;
let scrapeStartTime = 0;

function canCallBackend() {
  return Date.now() >= backendCooldownUntil;
}

function markBackendUnavailable(message = "Backend is offline. Start server with run.txt command.") {
  backendCooldownUntil = Date.now() + 5000;
  setStatus(message);
}

// Mode descriptions for the UI
const modeDescriptions = {
  ultra: `<small><strong>Ultra Deep:</strong> Uses ALL extraction engines (business_extractor, email_extractor, enhanced_scraper, deep_scraper) in parallel with cross-verification. Highest accuracy, slowest speed. Best for important lead generation.</small>`,
  deep: `<small><strong>Deep:</strong> Multi-source extraction - Google Maps → Website analysis → Google Search cross-verification. Finds Instagram, Facebook, WhatsApp and emails from multiple sources.</small>`,
  enhanced: `<small><strong>Enhanced:</strong> Google Maps + comprehensive website analysis. Extracts tech stack, chatbots, analytics. Good balance of speed and data quality.</small>`,
  basic: `<small><strong>Basic:</strong> Fast Maps-only extraction. Gets name, phone, address, rating, website from Google Maps only. Fastest option when you need quick results.</small>`
};

function updateModeDescription() {
  const mode = extractionModeSelect.value;
  if (modeDescription && modeDescriptions[mode]) {
    modeDescription.innerHTML = modeDescriptions[mode];
  }
}

// Initialize mode description
if (extractionModeSelect) {
  extractionModeSelect.addEventListener("change", updateModeDescription);
  updateModeDescription();
}

// ============================================================================
// HISTORY MANAGEMENT
// ============================================================================

async function fetchHistoryStats() {
  if (!canCallBackend()) {
    return;
  }

  const keyword = document.getElementById("keyword").value.trim();
  const location = document.getElementById("location").value.trim();
  
  if (!keyword || !location) {
    historyInfo.style.display = "none";
    return;
  }
  
  try {
    const res = await fetch(`/history/stats?keyword=${encodeURIComponent(keyword)}&location=${encodeURIComponent(location)}`);
    const data = await res.json();
    
    if (data.search_total > 0) {
      historyCount.textContent = data.search_total;
      historyInfo.style.display = "block";
    } else {
      historyInfo.style.display = "none";
    }
  } catch {
    markBackendUnavailable();
    historyInfo.style.display = "none";
  }
}

async function clearHistory() {
  if (!canCallBackend()) {
    markBackendUnavailable();
    return;
  }

  const keyword = document.getElementById("keyword").value.trim();
  const location = document.getElementById("location").value.trim();
  
  if (!keyword || !location) {
    alert("Please enter keyword and location first.");
    return;
  }
  
  if (!confirm(`Clear history for "${keyword}" in "${location}"?\n\nThis will allow you to scrape the same businesses again.`)) {
    return;
  }
  
  try {
    const res = await fetch("/history/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keyword, location }),
    });
    const data = await res.json();
    
    if (data.ok) {
      setStatus(`✓ ${data.message}`);
      historyInfo.style.display = "none";
    } else {
      setStatus(`Error: ${data.error || "Failed to clear history"}`);
    }
  } catch {
    markBackendUnavailable();
    setStatus("Error clearing history.");
  }
}

async function fetchOutputHistoryFiles() {
  if (!canCallBackend()) {
    return;
  }

  if (!historyFilesList) {
    return;
  }

  try {
    const res = await fetch("/history/output-files");
    const data = await res.json();
    outputHistoryFiles = Array.isArray(data.files) ? data.files : [];

    const availableNames = new Set(outputHistoryFiles.map((file) => file.name));
    for (const selected of [...selectedHistoryFiles]) {
      if (!availableNames.has(selected)) {
        selectedHistoryFiles.delete(selected);
      }
    }

    renderOutputHistoryFiles();
  } catch {
    markBackendUnavailable();
    historyFilesList.innerHTML = "Could not load output history files.";
  }
}

function renderOutputHistoryFiles() {
  if (!historyFilesList) {
    return;
  }

  historyFilesList.innerHTML = "";

  if (!outputHistoryFiles.length) {
    historyFilesList.textContent = "No output CSV files found yet.";
    return;
  }

  const fragment = document.createDocumentFragment();

  outputHistoryFiles.forEach((file) => {
    const row = document.createElement("label");
    row.className = "history-file-row";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedHistoryFiles.has(file.name);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        selectedHistoryFiles.add(file.name);
      } else {
        selectedHistoryFiles.delete(file.name);
      }
    });

    const nameEl = document.createElement("span");
    nameEl.className = "history-file-name";
    nameEl.textContent = file.name;

    const metaEl = document.createElement("small");
    metaEl.className = "history-file-meta";
    metaEl.textContent = `${file.rows || 0} rows | ${file.modified || "unknown"}`;

    row.appendChild(checkbox);
    row.appendChild(nameEl);
    row.appendChild(metaEl);
    fragment.appendChild(row);
  });

  historyFilesList.appendChild(fragment);
}

function getSelectedHistoryFiles() {
  return [...selectedHistoryFiles];
}

// Set up history listeners
if (clearHistoryBtn) {
  clearHistoryBtn.addEventListener("click", clearHistory);
}

if (refreshHistoryFilesBtn) {
  refreshHistoryFilesBtn.addEventListener("click", fetchOutputHistoryFiles);
}

if (selectAllHistoryFilesBtn) {
  selectAllHistoryFilesBtn.addEventListener("click", () => {
    outputHistoryFiles.forEach((file) => selectedHistoryFiles.add(file.name));
    renderOutputHistoryFiles();
  });
}

if (clearSelectedHistoryFilesBtn) {
  clearSelectedHistoryFilesBtn.addEventListener("click", () => {
    selectedHistoryFiles.clear();
    renderOutputHistoryFiles();
  });
}

// Check history when keyword/location changes
const keywordInput = document.getElementById("keyword");
const locationInput = document.getElementById("location");

function normalizeLocationText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9,\s-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function buildLocationFormatSuggestions(rawValue) {
  const cleaned = String(rawValue || "").replace(/\s+/g, " ").trim();
  if (!cleaned) {
    return [];
  }

  const options = [
    `${cleaned}, State/Region, Country`,
    `${cleaned}, Country`,
    `${cleaned} metro area, Country`,
  ];

  if (cleaned.includes(",")) {
    return options.slice(0, 1);
  }

  return options;
}

function hideLocationSuggestions() {
  if (!locationSuggestions) {
    return;
  }
  locationSuggestions.innerHTML = "";
  locationSuggestions.style.display = "none";
}

function showLocationSuggestions(inputValue, options) {
  if (!locationSuggestions || !locationInput || !Array.isArray(options) || options.length === 0) {
    hideLocationSuggestions();
    return;
  }

  locationSuggestions.innerHTML = "";

  const title = document.createElement("small");
  title.className = "location-suggestions-title";
  title.textContent = `Select exact location for "${inputValue}" (city/state/country):`;

  const list = document.createElement("div");
  list.className = "location-suggestions-list";

  options.forEach((option) => {
    const optionLabel = typeof option === "string" ? option : (option.label || option.value || "");
    const optionValue = typeof option === "string" ? option : (option.value || option.label || "");
    const optionHint = typeof option === "string" ? "" : (option.display_name || "");
    if (!optionValue) {
      return;
    }

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "location-suggestion-btn";
    btn.textContent = optionLabel;
    if (optionHint) {
      btn.title = optionHint;
    }
    btn.addEventListener("click", () => {
      locationInput.value = optionValue;
      hideLocationSuggestions();
      locationInput.dispatchEvent(new Event("input", { bubbles: true }));
      setStatus(`Location selected: ${optionLabel}`);
    });
    list.appendChild(btn);
  });

  locationSuggestions.appendChild(title);
  locationSuggestions.appendChild(list);
  locationSuggestions.style.display = "block";
}

async function fetchLocationSuggestions(raw) {
  if (!canCallBackend()) {
    return [];
  }

  if (locationSuggestController) {
    try {
      locationSuggestController.abort();
    } catch {
      // Ignore abort errors.
    }
  }

  locationSuggestController = new AbortController();

  try {
    const res = await fetch(
      `/location/suggest?q=${encodeURIComponent(raw)}&limit=${LOCATION_SUGGESTION_LIMIT}`,
      { signal: locationSuggestController.signal }
    );
    if (!res.ok) {
      return [];
    }
    const data = await res.json();
    return Array.isArray(data?.suggestions) ? data.suggestions : [];
  } catch (err) {
    if (err?.name === "AbortError") {
      return [];
    }
    return [];
  } finally {
    locationSuggestController = null;
  }
}

async function updateLocationDisambiguation() {
  if (!locationInput) {
    return;
  }

  const raw = locationInput.value.trim();
  if (!raw) {
    hideLocationSuggestions();
    return;
  }

  const fallbackSuggestions = buildLocationFormatSuggestions(raw);
  const normalizedRaw = normalizeLocationText(raw);

  const backendSuggestions = raw.length >= 2 ? await fetchLocationSuggestions(raw) : [];
  const combined = [];
  const seen = new Set();

  backendSuggestions.forEach((item) => {
    const value = normalizeLocationText(item?.value || item?.label || "");
    if (!value || seen.has(value)) {
      return;
    }
    seen.add(value);
    combined.push(item);
  });

  fallbackSuggestions.forEach((value) => {
    const key = normalizeLocationText(value);
    if (!key || seen.has(key)) {
      return;
    }
    seen.add(key);
    combined.push(value);
  });

  if (!combined || combined.length === 0) {
    hideLocationSuggestions();
    return;
  }

  const alreadySelected = combined.some((item) => {
    if (typeof item === "string") {
      return normalizeLocationText(item) === normalizedRaw;
    }
    return normalizeLocationText(item.value || item.label || "") === normalizedRaw;
  });
  if (alreadySelected) {
    hideLocationSuggestions();
    return;
  }

  showLocationSuggestions(raw, combined.slice(0, LOCATION_SUGGESTION_LIMIT));
}

if (keywordInput && locationInput) {
  let historyTimeout;
  const checkHistory = () => {
    clearTimeout(historyTimeout);
    historyTimeout = setTimeout(fetchHistoryStats, 500);
  };
  
  keywordInput.addEventListener("input", checkHistory);
  locationInput.addEventListener("input", () => {
    checkHistory();
    clearTimeout(locationSuggestTimer);
    locationSuggestTimer = setTimeout(() => {
      updateLocationDisambiguation();
    }, 250);
  });
  locationInput.addEventListener("focus", () => {
    clearTimeout(locationSuggestTimer);
    locationSuggestTimer = setTimeout(() => {
      updateLocationDisambiguation();
    }, 120);
  });
  locationInput.addEventListener("blur", () => {
    setTimeout(hideLocationSuggestions, 150);
  });
}

document.addEventListener("click", (event) => {
  if (!locationSuggestions || !locationInput) {
    return;
  }

  const target = event.target;
  if (locationSuggestions.contains(target) || locationInput.contains(target)) {
    return;
  }

  hideLocationSuggestions();
});

function setStatus(message) {
  statusEl.textContent = message;
}

function setRunningState(isRunning) {
  startBtn.disabled = isRunning;
  stopBtn.disabled = !isRunning;
}

function updateStats(rows) {
  if (!rows || rows.length === 0) {
    statsPanel.style.display = "none";
    return;
  }
  
  statsPanel.style.display = "block";
  document.getElementById("statTotal").textContent = rows.length;
  document.getElementById("statWithEmail").textContent = rows.filter(r => r.email).length;
  document.getElementById("statWithWhatsapp").textContent = rows.filter(r => r.whatsapp).length;
  document.getElementById("statWithInstagram").textContent = rows.filter(r => r.instagram).length;
  document.getElementById("statWithFacebook").textContent = rows.filter(r => r.facebook).length;
  document.getElementById("statWithWebsite").textContent = rows.filter(r => r.has_website === "Yes").length;
  
  // Handle both verified count and high quality count
  const verifiedEl = document.getElementById("statVerified");
  const highQualityEl = document.getElementById("statHighQuality");
  
  if (verifiedEl) {
    const verifiedCount = rows.filter(r => r.verified === true || r.verification_score > 50).length;
    verifiedEl.textContent = verifiedCount;
  }
  if (highQualityEl) {
    highQualityEl.textContent = rows.filter(r => r.quality_score === "high").length;
  }
}

function updateProgressBar(current, target) {
  const bar = document.getElementById("progressBar");
  const text = document.getElementById("progressText");
  const stats = document.getElementById("progressStats");
  if (!bar || !text) return;

  const pct = target > 0 ? Math.min(100, Math.floor((current / target) * 100)) : 0;
  bar.style.width = pct + "%";
  text.textContent = `${current} / ${target} (${pct}%)`;

  if (stats && scrapeStartTime) {
    const elapsed = ((Date.now() - scrapeStartTime) / 1000).toFixed(1);
    const rate = current > 0 ? (elapsed / current).toFixed(1) : "0";
    stats.textContent = `Elapsed: ${elapsed}s | ~${rate}s per lead`;
  }
}

function truncate(str, maxLen) {
  if (!str) return "";
  return str.length > maxLen ? str.substring(0, maxLen) + "..." : str;
}

function renderRows(rows) {
  bodyEl.innerHTML = "";
  updateStats(rows);

  if (!rows || rows.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td colspan='11'>No results yet.</td>";
    bodyEl.appendChild(tr);
    lastRenderedCount = 0;
    return;
  }

  // For performance on large results: use DocumentFragment
  const fragment = document.createDocumentFragment();

  rows.forEach((row) => {
    const tr = document.createElement("tr");

    const addressCell = row.address ? `<span title="${row.address}">${truncate(row.address, 25)}</span>` : "—";
    const websiteCell = row.website
      ? `<a href="${row.website}" target="_blank" rel="noopener noreferrer">🔗</a>`
      : "—";

    // Rich Web Info (new powerful fields)
    const webInfo = [];
    if (row.web_description) webInfo.push(`📝 ${truncate(row.web_description, 40)}`);
    if (row.web_services) webInfo.push(`🛠 ${truncate(row.web_services, 30)}`);
    if (row.web_hours) webInfo.push(`⏰ ${row.web_hours}`);
    const webInfoCell = webInfo.length ? webInfo.join("<br>") : (row.web_about ? truncate(row.web_about, 50) : "—");

    // Combined socials
    const socialsCell = [
      row.instagram ? `<a href="${row.instagram}" target="_blank">📸</a>` : "",
      row.facebook ? `<a href="${row.facebook}" target="_blank">👤</a>` : "",
      row.twitter ? `<a href="${row.twitter}" target="_blank">🐦</a>` : "",
      row.linkedin ? `<a href="${row.linkedin}" target="_blank">💼</a>` : ""
    ].filter(Boolean).join(" ") || "—";

    const ratingCell = row.rating ? `⭐ ${row.rating}` : "—";
    const qualityClass = row.quality_score === "high" ? "quality-high" : row.quality_score === "medium" ? "quality-medium" : "quality-low";
    const qualityCell = `<span class="quality ${qualityClass}">${(row.quality_score || "?").toUpperCase()}</span>`;

    const emailCell = row.email || (row.all_emails ? truncate(row.all_emails.split(";")[0], 20) : "—");
    const phoneCell = row.phone || (row.all_phones ? truncate(row.all_phones.split(";")[0], 15) : "—");

    const whatsappCell = row.whatsapp || "—";
    const waLink = row.whatsapp ? `<a href="https://wa.me/${row.whatsapp.replace(/[^0-9]/g, "")}" target="_blank">💬</a>` : "—";

    tr.innerHTML = `
      <td title="${row.name || ''}">${truncate(row.name, 22) || "—"}</td>
      <td>${addressCell}</td>
      <td>${phoneCell}</td>
      <td>${whatsappCell}</td>
      <td>${emailCell}</td>
      <td>${websiteCell}</td>
      <td style="font-size:0.8em; max-width:220px;">${webInfoCell}</td>
      <td>${socialsCell}</td>
      <td>${ratingCell}</td>
      <td>${qualityCell}</td>
      <td>
        ${waLink}
        ${row.website ? `<a href="${row.website}" target="_blank">🌐</a>` : ""}
      </td>
    `;

    fragment.appendChild(tr);
  });

  bodyEl.appendChild(fragment);
  lastRenderedCount = rows.length;
}

function normalizeMaxResults(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 50;
  }
  return Math.min(MAX_RESULTS_LIMIT, Math.max(1, Math.trunc(numeric)));
}

async function fetchStatus() {
  if (!canCallBackend()) {
    return;
  }

  try {
    const res = await fetch("/status");
    const data = await res.json();

    if (Array.isArray(data?.results) && data.results.length !== lastRenderedCount) {
      renderRows(data.results);
      downloadBtn.disabled = !(data.results.length > 0);
    } else if (currentTarget > 0 && data?.count !== undefined) {
      // Update progress even without full re-render
      updateProgressBar(data.count || 0, currentTarget);
    }

    if (data?.message) {
      setStatus(data.message);
    }

    if (data && data.running === false && pollingId) {
      setRunningState(false);
      stopPolling();
      stopRequestedByUser = false;
      activeScrapeController = null;
      downloadBtn.disabled = !(data.count > 0);
      fetchHistoryStats();
      fetchOutputHistoryFiles();

      // Hide or finalize progress bar
      const progressSection = document.getElementById("progressSection");
      if (progressSection) {
        setTimeout(() => { progressSection.style.display = "none"; }, 2500);
      }
      currentTarget = 0;
    }
  } catch {
    markBackendUnavailable("Backend connection lost. Please restart server.");
    setRunningState(false);
    stopPolling();
    stopRequestedByUser = false;
    activeScrapeController = null;
  }
}

function startPolling() {
  stopPolling();
  pollingId = setInterval(fetchStatus, 2000);
}

function stopPolling() {
  if (pollingId) {
    clearInterval(pollingId);
    pollingId = null;
  }
}

startBtn.addEventListener("click", async () => {
  const keyword = document.getElementById("keyword").value.trim();
  const location = document.getElementById("location").value.trim();
  const maxResultsInput = document.getElementById("maxResults");
  const maxResults = normalizeMaxResults(maxResultsInput.value || 50);
  const websiteFilter = document.getElementById("websiteFilter").value || "all";
  const extractionMode = document.getElementById("extractionMode").value || "deep";
  const headless = document.getElementById("headless").checked;
  const deepSearch = document.getElementById("deepSearch").checked;
  const verifySocials = document.getElementById("verifySocials")?.checked ?? true;
  const skipDuplicates = document.getElementById("skipDuplicates")?.checked ?? true;
  const dryRun = document.getElementById("dryRun")?.checked ?? false; // test 100s instantly & safely
  const chosenHistoryFiles = getSelectedHistoryFiles();

  maxResultsInput.value = String(maxResults);

  if (!keyword || !location) {
    setStatus("Keyword and location are required.");
    return;
  }

  // Update status based on mode
  const modeNames = {
    ultra: "🚀 Ultra Deep: ALL engines + Cross-verification",
    deep: "🔍 Deep: Maps → Website → Google Search",
    enhanced: "⚙️ Enhanced: Maps + Website analysis",
    basic: "⚡ Basic: Maps only (fast)"
  };
  
  let statusMsg = modeNames[extractionMode] || "Scraping...";
  if (skipDuplicates) {
    statusMsg += " (skipping previous results)";
  }
  if (chosenHistoryFiles.length > 0) {
    statusMsg += ` + ${chosenHistoryFiles.length} selected history file(s)`;
  }
  
  setRunningState(true);
  downloadBtn.disabled = true;
  renderRows([]);
  stopRequestedByUser = false;
  activeScrapeController = new AbortController();
  setStatus(statusMsg);

  // Setup progress bar
  currentTarget = maxResults;
  scrapeStartTime = Date.now();
  const progressSection = document.getElementById("progressSection");
  if (progressSection) progressSection.style.display = "block";
  updateProgressBar(0, currentTarget);

  startPolling();

  try {
    const res = await fetch("/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: activeScrapeController.signal,
      body: JSON.stringify({
        keyword,
        location,
        max_results: maxResults,
        website_filter: websiteFilter,
        extraction_mode: extractionMode,
        deep_search: deepSearch,
        verify_socials: verifySocials,
        skip_duplicates: skipDuplicates,
        dry_run: dryRun,
        card_only: cardOnly,
        selected_history_files: chosenHistoryFiles,
        headless,
      }),
    });

    const data = await res.json();
    if (!res.ok) {
      setStatus(data.error || "Scraping failed.");
      stopRequestedByUser = false;
      return;
    }

    // Background mode: initial response is "started", live results arrive via polling
    if (data && (data.status === "started" || data.status === "running")) {
      setStatus(data.message || "Scrape started... monitoring progress");
      // polling already active and will render live + final
    } else {
      renderRows(data.results || []);
      setStatus(data.message || `Completed. ${data.count || 0} leads collected.`);
      downloadBtn.disabled = !(data.count > 0);
    }
    stopRequestedByUser = false;
    
    // Refresh history stats after scraping
    fetchHistoryStats();
    fetchOutputHistoryFiles();
  } catch (err) {
    if (err?.name === "AbortError" && stopRequestedByUser) {
      setStatus("Stopping... returning collected results.");
      startPolling();
    } else {
      markBackendUnavailable();
      setStatus("Network error while scraping. Check backend logs.");
      stopRequestedByUser = false;
    }
  } finally {
    activeScrapeController = null;
    // Do NOT stop polling here: the scrape runs in background.
    // Polling self-stops when /status reports running=false.
    // Keep setRunningState true until polling confirms completion.
  }
});

stopBtn.addEventListener("click", async () => {
  if (!canCallBackend()) {
    markBackendUnavailable();
    return;
  }

  stopRequestedByUser = true;

  if (activeScrapeController) {
    try {
      activeScrapeController.abort();
    } catch {
      // Ignore abort errors.
    }
  }

  startPolling();

  try {
    const res = await fetch("/stop", { method: "POST" });
    const data = await res.json();

    if (Array.isArray(data?.results)) {
      renderRows(data.results);
      downloadBtn.disabled = !(data.results.length > 0);
    }

    setStatus(data.message || "Stop requested.");
  } catch {
    markBackendUnavailable();
    setStatus("Could not send stop request.");
  }
});

downloadBtn.addEventListener("click", () => {
  window.location.href = "/download";
});

renderRows([]);
fetchOutputHistoryFiles();
