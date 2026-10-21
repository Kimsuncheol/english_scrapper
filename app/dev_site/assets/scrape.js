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

function renderExpressions(items) {
  expressions.replaceChildren();
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "saved-expression";
    const title = document.createElement("h3");
    title.textContent = item.expression || "Expression needs OCR review";
    const meaning = document.createElement("p");
    meaning.textContent = item.korean_meaning || "";
    const headline = document.createElement("p");
    headline.textContent = `Publisher headline: ${item.article.title || "Untitled lesson"}`;
    const state = document.createElement("p");
    state.className = "muted";
    state.textContent = `Published: ${item.article.published_at ? new Date(item.article.published_at).toLocaleString() : "Not supplied"} · Processing: ${item.processing_status}`;
    const source = document.createElement("a");
    source.textContent = "Read source article";
    source.href = item.article.url;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    const raw = document.createElement("a");
    raw.textContent = "View saved OCR JSON";
    raw.href = `/api/v1/articles/${encodeURIComponent(item.id)}`;
    const links = document.createElement("p");
    links.className = "expression-links";
    links.append(source, raw);
    card.append(title, meaning, headline, state, links);
    if (item.processing_status === "partial") {
      const review = document.createElement("p");
      review.className = "muted";
      review.textContent = "Some lesson fields need review. Raw OCR is preserved in the saved JSON.";
      card.append(review);
    }
    expressions.append(card);
  }
}

async function loadSavedExpressions() {
  seriesRefresh.disabled = true;
  try {
    const response = await fetch(`${seriesBase}/saved`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "Could not load saved expressions.");
    renderExpressions(payload.items);
    savedStatus.textContent = payload.items.length
      ? `${payload.items.length} recently saved lesson(s), newest publication first.`
      : "No saved Speaking English lessons yet. Check for a new expression above.";
  } catch (error) {
    savedStatus.textContent = error.message || "Could not load saved expressions. Check the server connection.";
  } finally {
    seriesRefresh.disabled = false;
  }
}

seriesRefresh.addEventListener("click", loadSavedExpressions);
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
    if (payload.latest) renderExpressions([payload.latest]);
    await loadSavedExpressions();
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
