# Parser fixtures

These are small synthetic HTML samples modeled on the Chosun/Arc page-data
shapes described in the task; they are not captured publisher articles.
URLs under `example.test` are inert test data. Tests perform no network access.

- `fusion_lesson.html`: JSON-LD graph, full body images versus an OG thumbnail,
  resized variants, a narrow avatar, and an unrelated promo image.
- `fusion_only.html`: page-data metadata and a nested gallery, without JSON-LD.
- `og_fallback.html`: unsuitable body icon and a usable OG fallback.
- `malformed_fusion.html`: invalid JSON, comments, quoted assignments, and an
  executable expression that must never be interpreted.
- `no_image.html`: text-only article, invalid date, unrelated promo image.

## Lesson OCR fixtures

No lesson screenshots were attached to the task or present in the repository.
The following JSON fixtures are **synthetic layout examples**, not OCR exports
from supplied screenshots. They should be supplemented with verified screenshot
exports before claiming support for a particular publisher template.

- `lesson_ocr_single_column.json`: seven labeled fields, Korean explanation,
  straight/curly apostrophes, A/B labels, and multiline dialogue/translation.
- `lesson_ocr_two_columns.json`: Korean headings and parallel columns whose
  dialogue must not be interleaved with the expression/meaning column.
- `lesson_ocr_ambiguous.json`: unlabeled bilingual text that must remain raw;
  geometry alone is insufficient to infer semantic ownership.

Each fixture contains image dimensions and an OCR response with original region
order, text, confidence, and bounding boxes. Tests perturb geometry/confidence to
ensure the parser uses more than text matching and preserves the input exactly.
