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

// Presentation only: retain every nonblank OCR line in its original order.
// Only explicit section labels and speaker prefixes receive visual emphasis.
function ocrSpeakerPrefix(text, regions = [], leftEdge = 0) {
  const explicit = text.match(/^(\s*[AB]\s*[:：])(.*)$/i);
  if (explicit) return explicit;
  const spaced = text.match(/^(\s*[AB])(\s+.+)$/);
  if (!spaced) return null;
  // Real OCR sometimes omits the printed colon. Require a separate label at
  // the left margin, a visible word gap, and matching words on the same row.
  const label = spaced[1].trim();
  for (let i = 0; i < regions.length - 1; i += 1) {
    const region = regions[i], next = regions[i + 1];
    const a = region.bounding_box, b = next.bounding_box;
    if (region.text !== label || !a || !b || !next.text || !spaced[2].trimStart().startsWith(next.text)) continue;
    const overlap = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
    const gap = b.x - a.x - a.width;
    if (a.x <= leftEdge + 2 * a.height && overlap >= .45 * Math.min(a.height, b.height)
        && gap >= .6 * a.height && gap <= 3 * Math.max(a.height, b.height)) return spaced;
  }
  return null;
}

// Only known credit/advertising markers are excluded from the lesson view.
// Raw OCR regions and the saved API response remain intact for diagnostics.
function ocrFooterLine(text) {
  const compact = text.normalize("NFKC").replace(/\s/g, "").toLowerCase();
  return /^[(（]?ybm.*(?:센터|학원)/i.test(compact)
    || /^(?:학원문의|내용문의).*\d{2}.*\d{3,4}.*\d{4}/.test(compact);
}

function lessonTextLines(rawText) {
  return (rawText || "").split(/\r\n|\r|\n/).filter(text => !ocrFooterLine(text));
}

function ocrLineLayouts(lines, regions) {
  const compact = text => text.normalize("NFC").replace(/\s/g, "");
  let cursor = 0;
  return lines.map(text => {
    const target = compact(text);
    if (!target) return null;
    for (let start = cursor; start < Math.min(regions.length, cursor + 32); start += 1) {
      let joined = "";
      const words = [];
      for (let end = start; end < Math.min(regions.length, start + 256) && joined.length < target.length; end += 1) {
        joined += compact(regions[end].text);
        words.push(regions[end]);
        if (!target.startsWith(joined)) break;
        if (joined === target && words.every(word => word.bounding_box)) {
          cursor = end + 1;
          const boxes = words.map(word => word.bounding_box);
          return {x: Math.min(...boxes.map(box => box.x)),
            y: Math.min(...boxes.map(box => box.y)),
            bottom: Math.max(...boxes.map(box => box.y + box.height)),
            height: Math.max(...boxes.map(box => box.height)),
            bodyX: words.length > 1 && /^[AB][:：]?$/.test(words[0].text)
              ? words[1].bounding_box.x : boxes[0].x};
        }
      }
    }
    return null;
  });
}

function ocrReadingBlocks(rawText, regions = []) {
  const headings = /^(?:표현|영어 표현|뜻|의미|설명|해설|대화|영어 대화|대화 해석|대화 번역|해석|번역|어휘|단어|단어와 숙어|출처|Expression|Meaning|Explanation|Dialogue|Translation|Vocabulary|Vocabulary notes|Source credit)[:：]?$/i;
  const leftEdge = regions.reduce((min, region) => Math.min(min, region.bounding_box?.x ?? Infinity), Infinity);
  const sourceLines = (rawText || "").split(/\r\n|\r|\n/);
  const layouts = ocrLineLayouts(sourceLines, regions);
  const blocks = [];
  let current = null;
  let previousLayout = null;
  for (let index = 0; index < sourceLines.length; index += 1) {
    const text = sourceLines[index];
    if (ocrFooterLine(text)) { current = null; previousLayout = null; continue; }
    if (!text.trim()) { current = null; previousLayout = null; continue; }
    if (headings.test(text.trim())) {
      blocks.push({type: "heading", lines: [text]});
      current = null;
      previousLayout = null;
      continue;
    }
    const prefix = ocrSpeakerPrefix(text, regions, leftEdge);
    const layout = layouts[index];
    const korean = /[가-힣]/.test(text);
    const sameLanguage = current && current.korean === korean;
    const adjacent = !layout || !previousLayout || (
      layout.y - previousLayout.bottom <= 1.3 * Math.max(layout.height, previousLayout.height)
      && Math.abs(layout.x - current.bodyX) <= 1.5 * Math.max(layout.height, previousLayout.height));
    const vocabulary = /^[a-z][a-z'’~ -]*[:：]?\s+[가-힣]/i.test(text.trim());
    if (!current || prefix || !sameLanguage || !adjacent || vocabulary) {
      current = {type: prefix ? "dialogue" : "paragraph", lines: [], korean,
        bodyX: prefix ? layout?.bodyX : layout?.x};
      blocks.push(current);
    }
    current.lines.push(text);
    previousLayout = layout;
  }
  return blocks;
}

function renderOCRReadingView(ocr, expression) {
  const view = document.createElement("div");
  view.className = "ocr-reading-view";
  const blocks = ocrReadingBlocks(ocr.raw_text, ocr.regions);
  const leftEdge = ocr.regions.reduce((min, region) => Math.min(min, region.bounding_box?.x ?? Infinity), Infinity);
  if (!blocks.length) {
    const empty = document.createElement("p");
    empty.textContent = "OCR returned no lesson text.";
    view.append(empty);
  }
  for (const block of blocks) {
    const section = document.createElement(block.type === "heading" ? "h5" : "div");
    section.className = `ocr-reading-block ocr-${block.type}`;
    const text = block.lines.map(line => line.trim()).join(" ");
    if (block.type === "heading") {
      section.textContent = text;
      view.append(section);
      continue;
    }
    const line = document.createElement("p");
    line.className = "ocr-reading-line";
    const korean = /[가-힣]/.test(text);
    const english = /[a-z]/i.test(text);
    line.classList.add(korean ? "ocr-korean" : "ocr-english");
    if (korean && !english) line.lang = "ko";
    else if (english && !korean) line.lang = "en";
    if (expression && text === expression.trim()) line.classList.add("ocr-expression");
    const prefix = ocrSpeakerPrefix(text, ocr.regions, leftEdge);
    if (prefix) {
      const label = document.createElement("span");
      label.className = "ocr-speaker";
      label.textContent = prefix[1];
      const body = document.createElement("span");
      body.className = "ocr-dialogue-body";
      body.textContent = prefix[2].trimStart();
      line.append(label, body);
    } else line.textContent = text;
    section.append(line);
    view.append(section);
  }
  return view;
}

function renderExpressions(items) {
  expressions.replaceChildren();
  const fieldLabels = {
    korean_meaning: "Korean meaning",
    korean_explanation: "Korean explanation",
    english_dialogue: "English dialogue",
    korean_dialogue_translation: "Korean dialogue translation",
    vocabulary_notes: "Vocabulary notes",
    source_credit: "Source credit",
  };
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "saved-expression";
    const day = document.createElement("p");
    day.className = "eyebrow";
    day.textContent = item.issue_date ? `Lesson date: ${item.issue_date}` : "Lesson date not supplied";
    const title = document.createElement("h3");
    title.textContent = item.expression || "Expression needs OCR review";
    const headline = document.createElement("p");
    headline.textContent = `Publisher headline: ${item.article.title || "Untitled lesson"}`;
    const state = document.createElement("p");
    state.className = "muted";
    state.textContent = `Published: ${item.article.published_at ? new Date(item.article.published_at).toLocaleString() : "Not supplied"} · Processing: ${item.processing_status}`;
    const links = document.createElement("p");
    links.className = "expression-links";
    for (const [label, url] of [
      ["Read source article", item.article.url],
      ["Open original lesson image", item.lesson_image.url],
      ["View saved OCR JSON", `/api/v1/articles/${encodeURIComponent(item.id)}`],
    ]) {
      const link = document.createElement("a");
      link.textContent = label;
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      links.append(link);
    }
    card.append(day, title, headline, state, links);
    const fields = document.createElement("div");
    fields.className = "lesson-fields";
    for (const [key, label] of Object.entries(fieldLabels)) {
      const field = item.lesson.fields[key];
      const fieldText = field ? lessonTextLines(field.text).join("\n").trim() : "";
      if (key === "source_credit" && !fieldText) continue;
      const section = document.createElement("section");
      const heading = document.createElement("h4");
      heading.textContent = label;
      const text = document.createElement("p");
      text.className = field ? "lesson-field-text" : "muted";
      text.textContent = fieldText || "Not reliably classified; see full OCR text above.";
      section.append(heading, text);
      fields.append(section);
    }
    const rawHeading = document.createElement("h4");
    rawHeading.textContent = "Full OCR text";
    const rawNote = document.createElement("p");
    rawNote.className = "muted";
    rawNote.textContent = "OCR lines are reflowed into paragraphs and complete speaker turns. Publisher credits and advertising are excluded from this lesson view. Recognition errors may remain; compare the source image when needed.";
    const raw = document.createElement("pre");
    raw.className = "lesson-raw-text";
    raw.textContent = lessonTextLines(item.ocr.raw_text).join("\n") || "OCR returned no lesson text.";
    const original = document.createElement("details");
    original.className = "ocr-original";
    const originalHeading = document.createElement("summary");
    originalHeading.textContent = "Plain lesson text · original line breaks";
    original.append(originalHeading, raw);
    const reader = renderOCRReadingView(item.ocr, item.expression);
    const reasons = document.createElement("p");
    reasons.className = "muted";
    reasons.textContent = item.lesson.reasons.join(" ");
    const regions = document.createElement("details");
    const regionHeading = document.createElement("summary");
    regionHeading.textContent = `OCR regions, confidence and bounding boxes (${item.ocr.regions.length})`;
    const regionText = document.createElement("pre");
    regions.append(regionHeading, regionText);
    regions.addEventListener("toggle", () => {
      if (regions.open && !regionText.textContent) regionText.textContent = JSON.stringify(item.ocr.regions, null, 2);
    });
    // Put the complete readable lesson before optional classification/debugging.
    // Unassigned sections must not bury the available text under empty panels.
    const organized = document.createElement("details");
    const organizedHeading = document.createElement("summary");
    organizedHeading.textContent = "Organized lesson fields and classification status";
    organized.open = Object.keys(fieldLabels).some(key => Boolean(item.lesson.fields[key]));
    organized.append(organizedHeading, fields);
    card.append(rawHeading, rawNote, reader, original, reasons, organized, regions);
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
    savedStatus.textContent = `${seriesItems.length} saved lesson(s) loaded. Full OCR text is displayed for each day.`
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
    // A slower history request must not erase the explicitly selected day.
    historyRequest += 1;
    seriesRefresh.disabled = false;
    seriesMore.disabled = false;
    // Retain this record even if an old cached result falls outside page one.
    seriesItems = [item, ...seriesItems.filter(saved => saved.id !== item.id)];
    seriesDate.value = "";
    showLoadedDays();
    seriesDate.value = item.issue_date || "";
    showLoadedDays();
    savedStatus.textContent = `${seriesItems.length} saved lesson(s) loaded. Use the date selector to switch days.`;
    seriesStatus.className = "success";
    seriesStatus.textContent = `Loaded lesson for ${item.issue_date || "the selected day"}. Full OCR text is displayed below.`;
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
      savedStatus.textContent = `${seriesItems.length} saved lesson(s) loaded. Full OCR text is displayed for each day.`;
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
let archivePage = 1;
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
loadArchive();
