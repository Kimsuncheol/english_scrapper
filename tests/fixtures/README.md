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
- `daily_discovery.html`: synthetic English-section anchors and Fusion content
  cache stories, including duplicate URLs, another series, and rejected hosts.

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

### Wholly original six-section lesson

`lesson_ocr_original_six_sections.json` uses short original sample text about
making room for a chair. It was authored as mock OCR data: no article, publisher
image, attached screenshot, OCR export, network request or OCR model was used.
It is independent of the separately documented real-heading regression below.

The tall 720 × 1440 synthetic canvas contains these six sections, with Korean
section labels and English/Korean content:

| Field | Original sample |
| --- | --- |
| English expression | `Let's make room.` |
| Korean meaning | `자리를 마련하자.` |
| Korean explanation | `함께 쓸 공간을 만들자고 제안하는 말입니다.` |
| English dialogue | A/B turns about moving a bag and a blue box, including an indented continuation line |
| Korean translation | Matching A/B turns and an indented continuation line |
| Vocabulary | `make room`, `chair`, `blue box` with Korean glosses |

All 42 mock regions have confidence and pixel bounding boxes measured from the
top-left of that canvas (`x` increases rightward, `y` downward). The region array
is intentionally shuffled. `expected.reading_order` lists its indices in geometric
reading order; `expected.lines` records line text and constituent region indices.
`expected.fields` specifies exact six-field text and supporting region indices.
There is no source credit to infer, so its expected value is explicitly null.

Straight/curly apostrophes, Korean characters, punctuation, speaker labels and
multi-line text are preserved. `tests/test_synthetic_lesson.py` checks this contract,
reorders regions, degrades confidence, and removes supporting boxes. Its guards
fail if a test attempts OCR initialization or HTTP access. Run it directly with:

```sh
.venv/bin/python -m pytest tests/test_synthetic_lesson.py -q
```

### Series heading regression

`lesson_ocr_series_heading.json` includes only the five title OCR regions observed
from the [user-supplied October 21 article](https://www.chosun.com/national/nie/english/2026/10/21/I2PQNZFBDZHH7PTL7LILUZRDHM/),
plus synthetic context regions. The full publisher image and article text are not
stored in this fixture. It covers a centered, enlarged, unlabeled expression
whose recognized words match the series article title. Tests deliberately change
text, confidence, position and size to ensure metadata cannot invent missing OCR.
