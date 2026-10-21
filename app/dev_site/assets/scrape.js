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
  } catch {
    status.className = "error";
    status.textContent = "Could not read the API response. Check that the local server is running before retrying.";
  } finally {
    submit.disabled = false;
    form.removeAttribute("aria-busy");
  }
});
