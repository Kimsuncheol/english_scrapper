"""Check saved study content, independently of catalog/title visibility."""

CORE_FIELDS = ('expression', 'korean_meaning', 'korean_explanation',
               'english_dialogue', 'korean_dialogue_translation')


def missing_sections(record):
    payload = (record or {}).get('payload', record or {})
    fields = (payload.get('lesson') or {}).get('fields') or {}
    missing = []
    for name in CORE_FIELDS:
        value = fields.get(name)
        text = value.get('text') if isinstance(value, dict) else None
        if not isinstance(text, str) or not text.strip() or '[unreadable]' in text.lower():
            missing.append(name)
    if not (payload.get('ocr') or {}).get('raw_text', '').strip():
        missing.append('transcript')
    return missing
