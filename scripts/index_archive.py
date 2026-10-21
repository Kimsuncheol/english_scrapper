"""Index every publisher archive page into Firestore, without running OCR.

Run from the repository root: python -m scripts.index_archive
Each page is committed separately; --resume skips already indexed pages.
No web-service background timer is needed, and interruption loses no saved page.
"""

import argparse
import asyncio
import os

from app.services.archive import sync_archive_page
from app.services.storage import FirestoreStore
from app.services.errors import ArticleFetchError


async def run(resume=False):
    store = FirestoreStore(os.environ["FIREBASE_PROJECT_ID"], os.getenv("FIRESTORE_DATABASE", "(default)"))
    try:
        first = await sync_archive_page(store, 1)
        await store.initialize()
        state = (await store.collection("archive_state").document("series").get()).to_dict()
        done = set(state.get("indexed_page_numbers", [])) if resume else {1}
        for page in range(2, first["expected_pages"] + 1):
            if page in done:
                continue
            await asyncio.sleep(1)
            result = await sync_archive_page(store, page)
            print(f"Indexed page {page}/{result['expected_pages']}", flush=True)
        total = await store.publish_archive()
        print(f"Archive indexed: {total} lessons; publisher reports {first['publisher_total']}.", flush=True)
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.resume))
    except ArticleFetchError as exc:
        raise SystemExit(f"Archive import stopped ({exc.code}). Successful pages are saved; retry with --resume.") from None
