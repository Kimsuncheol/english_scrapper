"use strict";

const lessonContainer = document.getElementById("lesson-content");
const lessonStatus = document.getElementById("lesson-page-status");
try {
  const lesson = JSON.parse(document.getElementById("lesson-data").textContent);
  document.getElementById("lesson-page-heading").textContent = lesson.issue_date
    ? `Lesson for ${lesson.issue_date}` : "Daily English lesson";
  renderLessonCards([lesson], lessonContainer);
  lessonStatus.textContent = lesson.ocr.engine?.startsWith("ollama/")
    ? `Saved lesson · Korean and English · AI image transcription (${lesson.ocr.engine})`
    : "Saved lesson · Korean and English · Full OCR reading view";
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.textContent = "Read image again with Ollama";
  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "This makes a new Ollama request. The previous saved result is kept as a Firestore revision.";
  refresh.addEventListener("click", async () => {
    if (refresh.disabled) return;
    refresh.disabled = true;
    lessonStatus.textContent = "Ollama is reading the original lesson image…";
    try {
      const response = await fetch(`/_dev/test-site/lessons/${encodeURIComponent(lesson.id)}/transcribe`, {method: "POST"});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || "Image transcription failed.");
      window.location.reload();
    } catch (error) {
      lessonStatus.className = "error";
      lessonStatus.textContent = error.message || "Could not transcribe the image. The previous saved result is preserved.";
      refresh.disabled = false;
    }
  });
  lessonContainer.before(refresh, note);
} catch {
  lessonStatus.className = "error";
  lessonStatus.textContent = "The saved lesson could not be displayed. Return to the homepage and try again.";
}
