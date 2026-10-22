"use strict";

const form = document.getElementById("scrape-form");
const submit = document.getElementById("scrape-submit");
const status = document.getElementById("scrape-status");
const result = document.getElementById("scrape-result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return;
  submit.disabled = true;
  form.setAttribute("aria-busy", "true");
  status.className = "";
  status.textContent = "Fetching article and running OCR. The first request may take longer…";
  result.hidden = true;
  result.textContent = "";
  try {
    const response = await fetch("/api/v1/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ article_url: document.getElementById("article-url").value.trim() }),
    });
    const payload = await response.json();
    result.textContent = JSON.stringify(payload, null, 2);
    result.hidden = false;
    if (!response.ok) {
      status.className = "error";
      status.textContent = `HTTP ${response.status}: ${payload.error?.message || "Scrape failed."}`;
      if (payload.error?.code === "storage_unavailable") {
        status.textContent += " Configure FIREBASE_PROJECT_ID and ensure Firestore credentials are configured.";
      }
      return;
    }
    status.className = "success";
    const expression = payload.lesson?.fields?.expression?.text;
    status.textContent = `HTTP ${response.status} · Processing: ${payload.processing?.status || "complete"}`
      + (expression ? ` · Expression: ${expression}` : " · No expression was reliably classified; review raw OCR below.");
    const savedLocation = response.headers.get("Location");
    if (savedLocation?.startsWith("/api/v1/articles/")) {
      const link = document.createElement("a");
      link.href = lessonPageHref(savedLocation.split("/").pop());
      link.textContent = " Open full lesson →";
      status.append(link);
    }
    await loadSavedExpressions();
    await loadArchive(1);
  } catch {
    status.className = "error";
    status.textContent = "Could not read the API response. Check that the local server is running before retrying.";
  } finally {
    submit.disabled = false;
    form.removeAttribute("aria-busy");
  }
});

const seriesCheck = document.getElementById("series-check");
const seriesRefresh = document.getElementById("series-refresh");
const seriesStatus = document.getElementById("series-status");
const savedStatus = document.getElementById("saved-status");
const expressions = document.getElementById("series-expressions");
const seriesBase = "/_dev/test-site/series";

const seriesMore = document.getElementById("series-more");
const seriesDate = document.getElementById("series-date");
const availableDays = document.getElementById("series-available");
const loadDay = document.getElementById("series-load-day");
let seriesItems = [];
let nextOffset = null;
let historyRequest = 0;

function lessonDate(item) {
  return item.issue_date || "";
}

function lessonPageHref(id) {
  return `/_dev/test-site/lessons/${encodeURIComponent(id)}?page=${archivePage}`;
}

function renderExpressions(items) {
  expressions.replaceChildren();
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "lesson-preview";
    const day = document.createElement("p");
    day.className = "eyebrow";
    day.textContent = item.issue_date || "Lesson date not supplied";
    const title = document.createElement("h3");
    const titleLink = document.createElement("a");
    titleLink.href = lessonPageHref(item.id);
    titleLink.textContent = item.expression || item.article.title || "English lesson";
    title.append(titleLink);
    const link = document.createElement("a");
    link.className = "lesson-open-link";
    link.href = lessonPageHref(item.id);
    link.textContent = "Read full lesson →";
    card.append(day, title);
    if (item.korean_meaning) {
      const meaning = document.createElement("p");
      meaning.textContent = item.korean_meaning;
      card.append(meaning);
    }
    card.append(link);
    expressions.append(card);
  }
}

function showLoadedDays() {
  const selected = seriesDate.value;
  const days = [...new Set(seriesItems.map(lessonDate).filter(Boolean))].sort().reverse();
  seriesDate.replaceChildren();
  for (const [value, label] of [["", "All loaded days"], ...days.map(day => [day, day])]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    seriesDate.append(option);
  }
  seriesDate.value = days.includes(selected) ? selected : "";
  seriesItems.sort((left, right) => lessonDate(right).localeCompare(lessonDate(left)) || right.saved_at.localeCompare(left.saved_at));
  renderExpressions(seriesItems.filter(item => !seriesDate.value || lessonDate(item) === seriesDate.value));
}

async function loadSavedExpressions(append = false) {
  const request = ++historyRequest;
  seriesRefresh.disabled = true;
  seriesMore.disabled = true;
  try {
    const offset = append ? nextOffset : 0;
    if (offset === null) return;
    const response = await fetch(`${seriesBase}/saved?limit=5&offset=${offset}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "Could not load saved expressions.");
    if (request !== historyRequest) return;
    const combined = append ? [...seriesItems, ...payload.items] : payload.items;
    seriesItems = [...new Map(combined.map(item => [item.id, item])).values()];
    nextOffset = payload.next_offset;
    seriesMore.hidden = nextOffset === null;
    showLoadedDays();
    savedStatus.textContent = `${seriesItems.length} saved lesson(s) loaded. Open a lesson to read its full OCR text.`
      + (nextOffset !== null ? " Load more saved days below to continue browsing." : " All saved records have been scanned.");
  } catch (error) {
    if (request === historyRequest) savedStatus.textContent = error.message || "Could not load saved expressions. Check the server connection.";
  } finally {
    if (request === historyRequest) {
      seriesRefresh.disabled = false;
      seriesMore.disabled = false;
    }
  }
}

seriesDate.addEventListener("change", showLoadedDays);
seriesRefresh.addEventListener("click", () => loadSavedExpressions());
seriesMore.addEventListener("click", () => loadSavedExpressions(true));

async function openArchiveLesson(articleUrl) {
  if (!articleUrl || submit.disabled || seriesCheck.disabled) return;
  loadDay.disabled = true;
  submit.disabled = true;
  seriesCheck.disabled = true;
  seriesStatus.className = "";
  seriesStatus.textContent = "Loading the selected day's complete lesson. Unsaved images require OCR…";
  try {
    const response = await fetch(`${seriesBase}/day`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ article_url: articleUrl }),
    });
    const item = await response.json();
    if (!response.ok) throw new Error(item.error?.message || "Could not load this day.");
    window.location.assign(lessonPageHref(item.id));
  } catch (error) {
    seriesStatus.className = "error";
    seriesStatus.textContent = error.message || "Could not load this day. Try again later.";
  } finally {
    loadDay.disabled = !availableDays.value;
    submit.disabled = false;
    seriesCheck.disabled = false;
  }
}

loadDay.addEventListener("click", () => openArchiveLesson(availableDays.value));

seriesCheck.addEventListener("click", async () => {
  if (seriesCheck.disabled || submit.disabled) return;
  seriesCheck.disabled = true;
  submit.disabled = true;
  seriesStatus.className = "";
  seriesStatus.textContent = "Checking the series page. A new lesson may take a moment to OCR and save…";
  try {
    const response = await fetch(`${seriesBase}/check`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "Could not check the publisher.");
    const messages = {
      new: "New lesson found and saved.",
      up_to_date: "You’re up to date: the newest listed lesson is already saved.",
      empty: "No matching lesson links were found. This does not confirm that no new lesson was published.",
    };
    seriesStatus.className = payload.status === "empty" ? "" : "success";
    seriesStatus.textContent = `${messages[payload.status]} Source checked: ${new Date(payload.checked_at).toLocaleString()}.`
      + (payload.discovery_cached ? " Using the 60-second discovery cache." : "");
    availableDays.replaceChildren();
    for (const day of payload.listed_days) {
      const option = document.createElement("option");
      option.value = day.article_url;
      option.textContent = day.issue_date || day.article_url;
      availableDays.append(option);
    }
    availableDays.disabled = !payload.listed_days.length;
    loadDay.disabled = availableDays.disabled;
    await loadSavedExpressions();
    await loadArchive(1);
    if (payload.latest) {
      seriesItems = [payload.latest, ...seriesItems.filter(item => item.id !== payload.latest.id)];
      seriesDate.value = "";
      showLoadedDays();
      savedStatus.textContent = `${seriesItems.length} saved lesson(s) loaded. Open a lesson to read its full OCR text.`;
    }
  } catch (error) {
    seriesStatus.className = "error";
    seriesStatus.textContent = error.message || "Could not check the publisher. Try again later.";
  } finally {
    seriesCheck.disabled = false;
    submit.disabled = false;
  }
});

// Read saved Firestore results only; publisher/OCR work requires an explicit click.
loadSavedExpressions();


const archiveStatus = document.getElementById("archive-status");
const archiveEntries = document.getElementById("archive-entries");
const archivePagination = document.getElementById("archive-pagination");
const requestedPage = Number(new URLSearchParams(window.location.search).get("page") || 1);
let archivePage = Number.isInteger(requestedPage) && requestedPage >= 1 && requestedPage <= 400 ? requestedPage : 1;
let archivePages = 0;
let archiveRequest = 0;

function renderArchivePagination() {
  archivePagination.replaceChildren();
  if (!archivePages) return;
  function button(label, page, disabled = false) {
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = label;
    control.disabled = disabled;
    if (typeof label === "number") {
      control.setAttribute("aria-label", `Page ${page}`);
      if (page === archivePage) control.setAttribute("aria-current", "page");
    }
    control.addEventListener("click", () => loadArchive(page));
    archivePagination.append(control);
  }
  button("Previous", archivePage - 1, archivePage <= 1);
  const pages = [...new Set([1, archivePages,
    ...Array.from({length: 5}, (_, index) => archivePage - 2 + index)
      .filter(page => page >= 1 && page <= archivePages)])].sort((a, b) => a - b);
  let previous = 0;
  for (const page of pages) {
    if (previous && page - previous > 1) {
      const gap = document.createElement("span");
      gap.className = "pagination-gap";
      gap.textContent = "…";
      archivePagination.append(gap);
    }
    button(page, page, page === archivePage);
    previous = page;
  }
  button("Next", archivePage + 1, archivePage >= archivePages);
  const position = document.createElement("span");
  position.className = "pagination-position";
  position.textContent = `Page ${archivePage} of ${archivePages} · 10 days per page`;
  archivePagination.append(position);
}

async function loadArchive(page = 1) {
  const request = ++archiveRequest;
  archiveStatus.textContent = "Loading the persistent expression archive…";
  archivePagination.querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    const params = new URLSearchParams({ page: String(page) });
    const response = await fetch(`${seriesBase}/archive?${params}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "Could not load the archive.");
    if (request !== archiveRequest) return;
    archivePage = data.page;
    window.history.replaceState(null, "", `${window.location.pathname}?page=${archivePage}`);
    archivePages = data.pages;
    archiveEntries.replaceChildren();
    for (const item of data.items) {
      const row = document.createElement("tr");
      const dayCell = document.createElement("td");
      dayCell.textContent = item.issue_date;
      const expressionCell = document.createElement("td");
      const source = document.createElement("a");
      source.href = item.article_url;
      source.textContent = item.expression;
      source.target = "_blank";
      source.rel = "noopener noreferrer";
      expressionCell.append(source);
      const action = document.createElement("td");
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "Open full lesson";
      open.setAttribute("aria-label", `Open full lesson for ${item.issue_date}`);
      open.addEventListener("click", async () => {
        open.disabled = true;
        try { await openArchiveLesson(item.article_url); }
        finally { open.disabled = false; }
      });
      action.append(open);
      row.append(dayCell, expressionCell, action);
      archiveEntries.append(row);
    }
    archiveStatus.className = "";
    archiveStatus.textContent = `${data.total.toLocaleString()} expression(s) in the archive. Page ${data.page} of ${data.pages}.`
      + ` Publisher: ${data.publisher_total ?? "unknown"} lessons; ${data.indexed_pages}/${data.expected_pages} source pages indexed.`
      + (data.checked_at ? ` Last indexed: ${new Date(data.checked_at).toLocaleString()}.` : " Run the archive indexer to populate Firestore.")
      + " Catalog titles are from the publisher; full lesson text is OCR when opened.";
    renderArchivePagination();
  } catch (error) {
    if (request !== archiveRequest) return;
    archiveStatus.className = "error";
    archiveStatus.textContent = error.message || "Could not load the archive. Try again later.";
    renderArchivePagination();
  }
}
loadArchive(archivePage);
