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
