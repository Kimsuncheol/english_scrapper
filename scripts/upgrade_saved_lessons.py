"""Audit saved lessons and explicitly upgrade legacy OCR, preserving revisions.

Reads never run inference. --apply uses the existing opt-in transcription route;
it processes sequentially, reports failures and never blindly retries AI.
"""
import argparse
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote


def request(base, path, *, post=False):
    req = Request(base.rstrip('/') + path, data=b'' if post else None)
    with urlopen(req, timeout=120) as response:
        return json.load(response)


def upgrade(base, *, apply=False):
    offset = 0
    failed = []
    while True:
        page = request(base, f'/api/v1/articles?limit=100&offset={offset}')
        for summary in page['items']:
            if not (summary.get('article', {}).get('title') or '').startswith('[스피킹이 강해지는 영어]'):
                continue
            identifier = quote(summary['id'], safe='')
            record = request(base, '/api/v1/articles/' + identifier)
            payload = record['payload']
            # Already migrated partial AI results need individual review; do not
            # repeatedly spend inference calls on absent/uncertain source text.
            if payload['ocr']['engine'].startswith('ollama/'):
                continue
            print(summary['id'], payload['ocr']['engine'], 'upgrade' if apply else 'needs upgrade', flush=True)
            if apply:
                try:
                    result = request(base, '/_dev/test-site/lessons/' + identifier + '/transcribe', post=True)
                except (HTTPError, URLError, TimeoutError) as exc:
                    failed.append(summary['id'])
                    print('  failed:', getattr(exc, 'code', type(exc).__name__), flush=True)
                    continue
                print('  result:', result['ocr']['engine'], result['lesson']['status'], flush=True)
        offset += len(page['items'])
        if offset >= page['total'] or not page['items']:
            if failed:
                raise RuntimeError("Saved results preserved; failed IDs: " + ", ".join(failed))
            return


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    upgrade(args.base_url, apply=args.apply)
