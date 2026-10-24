"use strict";

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

// AI fields already reference validated transcript lines. Use those boundaries
// for presentation rather than trying to infer layout without bounding boxes.
function lessonDialogueTurns(text) {
  const turns = [];
  for (const line of lessonTextLines(text)) {
    if (!line.trim()) continue;
    const prefix = line.trim().match(/^([AB])(?:[:：]\s*|\s+)(.+)$/);
    if (prefix) turns.push({speaker: prefix[1], text: prefix[2]});
    else if (turns.length) turns[turns.length - 1].text += ` ${line.trim()}`;
    else turns.push({speaker: null, text: line.trim()});
  }
  return turns;
}

function renderStructuredReadingView(item) {
  const view = document.createElement("div");
  view.className = "ocr-reading-view lesson-structured-reader";
  const expression = document.createElement("strong");
  expression.className = "lesson-expression";
  expression.textContent = item.expression || item.lesson.fields.expression?.text || "Expression needs review";
  view.append(expression);
  const sections = [
    ["korean_meaning", "Meanings", "ko"],
    ["korean_explanation", "Explanation", "ko"],
    ["english_dialogue", "English", "en"],
    ["korean_dialogue_translation", "Korean translation", "ko"],
    ["vocabulary_notes", "Vocabulary", null],
  ];
  let examples = null;
  for (const [key, label, language] of sections) {
    const text = lessonTextLines(item.lesson.fields[key]?.text).join("\n").trim();
    if (!text) continue;
    const section = document.createElement("section");
    section.className = "lesson-reader-section";
    const heading = document.createElement("h5");
    heading.textContent = label;
    section.append(heading);
    if (language) section.lang = language;
    if (key.includes("dialogue")) {
      for (const turn of lessonDialogueTurns(text)) {
        const row = document.createElement("p");
        row.className = turn.speaker ? "lesson-reader-turn" : "lesson-reader-paragraph";
        if (turn.speaker) {
          const speaker = document.createElement("strong");
          speaker.className = "ocr-speaker";
          speaker.textContent = turn.speaker;
          row.append(speaker);
        }
        const body = document.createElement("span");
        body.textContent = turn.text;
        row.append(body);
        section.append(row);
      }
    } else {
      const paragraph = document.createElement("p");
      paragraph.className = "lesson-reader-paragraph";
      paragraph.textContent = text.replace(/\n/g, " ");
      section.append(paragraph);
    }
    if (key.includes("dialogue")) {
      if (!examples) {
        examples = document.createElement("section");
        examples.className = "lesson-reader-section";
        const heading = document.createElement("h5");
        heading.textContent = "Examples";
        examples.append(heading);
        view.append(examples);
      }
      examples.append(section);
    } else view.append(section);
  }
  // Keep unassigned transcript lines visible instead of dropping uncertain text.
  const assigned = new Set(sections.flatMap(([key]) => item.lesson.fields[key]?.line_indices || []));
  for (const i of item.lesson.fields.expression?.line_indices || []) assigned.add(i);
  const remaining = (item.ocr.lines || []).filter((line, i) => !assigned.has(i) && !ocrFooterLine(line));
  if (remaining.length) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Other transcribed text";
    const text = document.createElement("pre");
    text.textContent = remaining.join("\n");
    details.append(summary, text);
    view.append(details);
  }
  return view;
}

function renderLessonCards(items, container) {
  container.replaceChildren();
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
    const aiVision = item.ocr.engine?.startsWith("ollama/");
    rawHeading.textContent = aiVision ? "AI image transcription" : "Full OCR text";
    const rawNote = document.createElement("p");
    rawNote.className = "muted";
    rawNote.textContent = aiVision ? "Ollama read this lesson directly from its image. AI recognition can contain errors; compare the original image when correcting text." : "OCR lines are reflowed into paragraphs and complete speaker turns. Publisher credits and advertising are excluded from this lesson view. Recognition errors may remain; compare the source image when needed.";
    const raw = document.createElement("pre");
    raw.className = "lesson-raw-text";
    raw.textContent = lessonTextLines(item.ocr.raw_text).join("\n") || "OCR returned no lesson text.";
    const original = document.createElement("details");
    original.className = "ocr-original";
    const originalHeading = document.createElement("summary");
    originalHeading.textContent = "Plain lesson text · original line breaks";
    original.append(originalHeading, raw);
    const reader = item.lesson.fields.expression
      ? renderStructuredReadingView(item) : renderOCRReadingView(item.ocr, item.expression);
    const reasons = document.createElement("p");
    reasons.className = "muted";
    reasons.textContent = item.lesson.reasons.join(" ");
    const regions = document.createElement("details");
    const regionHeading = document.createElement("summary");
    regionHeading.textContent = aiVision ? "AI transcript lines · measured OCR boxes and confidence unavailable" : `OCR regions, confidence and bounding boxes (${item.ocr.regions.length})`;
    const regionText = document.createElement("pre");
    regions.append(regionHeading, regionText);
    regions.addEventListener("toggle", () => {
      if (regions.open && !regionText.textContent) regionText.textContent = JSON.stringify(aiVision ? item.ocr.lines : item.ocr.regions, null, 2);
    });
    // Put the complete readable lesson before optional classification/debugging.
    // Unassigned sections must not bury the available text under empty panels.
    const organized = document.createElement("details");
    const organizedHeading = document.createElement("summary");
    organizedHeading.textContent = "Organized lesson fields and classification status";
    organized.open = !aiVision && Object.keys(fieldLabels).some(key => Boolean(item.lesson.fields[key]));
    organized.append(organizedHeading, fields);
    card.append(rawHeading, rawNote, reader, original, reasons, organized, regions);
    container.append(card);
  }
}

