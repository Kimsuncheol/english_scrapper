"use strict";

const lessonContainer = document.getElementById("lesson-content");
const lessonStatus = document.getElementById("lesson-page-status");
try {
  const lesson = JSON.parse(document.getElementById("lesson-data").textContent);
  document.getElementById("lesson-page-heading").textContent = lesson.issue_date
    ? `Lesson for ${lesson.issue_date}` : "Daily English lesson";
  renderLessonCards([lesson], lessonContainer);
  lessonStatus.textContent = "Saved lesson · Korean and English · Full OCR reading view";
} catch {
  lessonStatus.className = "error";
  lessonStatus.textContent = "The saved lesson could not be displayed. Return to the homepage and try again.";
}
