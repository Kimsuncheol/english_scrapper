"""Cloud Firestore persistence with renewable, transaction-fenced leases."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import json
import random
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

from google.api_core.exceptions import Aborted, GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.services.discovery import stable_url
from app.services.errors import ArticleFetchError

STORAGE_ERRORS = (GoogleAPIError, GoogleAuthError, OSError)
RPC = {"timeout": 10, "retry": None}
LEASE_SECONDS = 120


class StorageUnavailable(ArticleFetchError):
    def __init__(self):
        super().__init__("storage_unavailable", "Persistent Firestore storage is unavailable.", 503)


def identifier(value):
    return str(uuid5(NAMESPACE_URL, value))


def clock(snapshot=None):
    # Use the server read timestamp for lease comparisons when available.
    return getattr(snapshot, "read_time", None) or datetime.now(timezone.utc)


class FirestoreStore:
    def __init__(self, project: str, database: str = "(default)", *, client=None, prefix="chosun"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", prefix):
            raise ValueError("Invalid Firestore collection prefix")
        self.project, self.database, self.prefix = project, database, prefix
        self._client = client

    async def initialize(self):
        if not self.project:
            raise StorageUnavailable()
        if self._client is None:
            try:
                # Lazy ADC lookup: /health never loads credentials or contacts Firebase.
                self._client = firestore.AsyncClient(project=self.project, database=self.database)
            except (GoogleAuthError, ValueError, OSError) as exc:
                raise StorageUnavailable() from exc

    async def close(self):
        if self._client is not None:
            # AsyncClient inherits a close() that only closes its HTTP helper;
            # explicitly close the initialized gRPC transport as well.
            api = getattr(self._client, "_firestore_api_internal", None)
            if api is not None:
                await api.transport.close()
            self._client.close()
            self._client = None

    def collection(self, name):
        return self._client.collection(f"{self.prefix}_{name}")

    async def transact(self, operation):
        await self.initialize()
        try:
            async with asyncio.timeout(30):
                for attempt in range(3):
                    @firestore.async_transactional
                    async def execute(transaction):
                        return await operation(transaction)
                    try:
                        return await execute(self._client.transaction(max_attempts=1))
                    except (Aborted, ValueError) as exc:
                        # The SDK wraps exhausted commit conflicts in ValueError;
                        # read conflicts can raise Aborted directly. Jitter avoids
                        # two canonical aliases repeatedly colliding in lockstep.
                        conflict = isinstance(exc, Aborted) or isinstance(exc.__cause__, Aborted)
                        if not conflict or attempt == 2:
                            raise
                        await asyncio.sleep(random.uniform(0.1, 0.3) * 2 ** attempt)
        except (*STORAGE_ERRORS, TimeoutError, ValueError) as exc:
            raise StorageUnavailable() from exc

    async def acquire(self, name, owner):
        ref = self.collection("locks").document(name)
        async def operation(tx):
            snapshot = await ref.get(transaction=tx, **RPC)
            row, now = snapshot.to_dict(), clock(snapshot)
            if row and row["expires_at"] > now:
                return False
            tx.set(ref, {"owner": owner, "expires_at": now + timedelta(seconds=LEASE_SECONDS)})
            return True
        return await self.transact(operation)

    @asynccontextmanager
    async def lease(self, name):
        await self.initialize()
        owner = uuid4().hex
        if not await self.acquire(name, owner):
            yield None
            return
        session = RunSession(self, name, owner)
        heartbeat = asyncio.create_task(session.heartbeat())
        try:
            yield session
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            # Never remove a successor's lease or mask the original error.
            with suppress(StorageUnavailable, ArticleFetchError):
                async def release(tx):
                    ref = self.collection("locks").document(name)
                    snapshot = await ref.get(transaction=tx, **RPC)
                    if snapshot.exists and snapshot.to_dict()["owner"] == owner:
                        tx.delete(ref)
                await self.transact(release)

    @asynccontextmanager
    async def run_session(self):
        async with self.lease("daily") as session:
            yield session

    @asynccontextmanager
    async def article_session(self, url):
        async with self.lease("article-" + identifier(stable_url(url))) as session:
            if session is None:
                raise ArticleFetchError("scrape_busy", "This article is being processed. Retry later.", 503)
            yield session

    async def status(self):
        await self.initialize()
        try:
            last = [s.to_dict() async for s in self.collection("runs").order_by("started_at", direction="DESCENDING").limit(1).stream(**RPC)]
            failures = [s.to_dict() async for s in self.collection("queue").where(filter=FieldFilter("state", "==", "failed")).order_by("last_attempt_at", direction="DESCENDING").limit(20).stream(**RPC)]
            return {"last_run": last[0] if last else None, "failures": failures}
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable() from exc

    async def list_lessons(self, limit=20, offset=0):
        await self.initialize()
        try:
            query = self.collection("lessons").select(["id", "canonical_url", "article", "processing_status", "created_at", "updated_at"]).order_by("created_at", direction="DESCENDING").order_by("__name__", direction="DESCENDING").offset(offset).limit(limit)
            return [s.to_dict() async for s in query.stream(**RPC)]
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable() from exc

    async def lesson(self, lesson_id):
        await self.initialize()
        # A slash cannot turn an API ID into a different document path.
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", lesson_id):
            return None
        try:
            snapshot = await self.collection("lessons").document(lesson_id).get(**RPC)
            row = snapshot.to_dict()
            if row:
                row["payload"] = json.loads(row["payload_json"])
            return row
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable() from exc

    async def articles(self, limit=20, offset=0):
        await self.initialize()
        try:
            count = await self.collection("lessons").count().get(**RPC)
            return {"items": await self.list_lessons(limit, offset), "total": count[0][0].value, "limit": limit, "offset": offset}
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable() from exc

    async def series_articles(self, limit=5, offset=0):
        """Page actual English article URLs by their padded YYYY/MM/DD path.

        Canonical URLs have one normalized host/prefix. This uses the existing
        single-field index, needs no new collection/index or data migration,
        and keeps a newly imported old day from displacing today's lesson.
        """
        await self.initialize()
        prefix = "https://www.chosun.com/national/nie/english/"
        try:
            query = self.collection("lessons").where(filter=FieldFilter("canonical_url", ">=", prefix)).where(filter=FieldFilter("canonical_url", "<", prefix + "\uf8ff"))
            count = await query.count().get(**RPC)
            page = query.select(["id", "canonical_url", "article", "processing_status", "created_at", "updated_at"]).order_by("canonical_url", direction="DESCENDING").offset(offset).limit(limit)
            return {"items": [s.to_dict() async for s in page.stream(**RPC)], "total": count[0][0].value}
        except STORAGE_ERRORS as exc:
            raise StorageUnavailable() from exc


class RunSession:
    def __init__(self, store, name, owner):
        self.store, self.name, self.owner = store, name, owner
        self.lost = False

    async def transaction(self, operation):
        async def guarded(tx):
            ref = self.store.collection("locks").document(self.name)
            snapshot = await ref.get(transaction=tx, **RPC)
            row = snapshot.to_dict()
            if self.lost or not row or row["owner"] != self.owner or row["expires_at"] <= clock(snapshot):
                raise ArticleFetchError("storage_lock_lost", "Processing lease expired. Retry later.", 503)
            return await operation(tx)
        return await self.store.transact(guarded)

    async def heartbeat(self):
        try:
            while True:
                await asyncio.sleep(30)
                async def renew(tx):
                    ref = self.store.collection("locks").document(self.name)
                    snapshot = await ref.get(transaction=tx, **RPC)
                    tx.update(ref, {"expires_at": clock(snapshot) + timedelta(seconds=LEASE_SECONDS)})
                await self.transaction(renew)
        except (StorageUnavailable, ArticleFetchError):
            self.lost = True

    async def check_connection(self):
        async def check(tx):
            return None
        await self.transaction(check)

    def queue(self, url):
        return self.store.collection("queue").document(identifier(stable_url(url)))

    async def cached(self, url):
        await self.check_connection()
        canonical = await self.store.lesson(identifier(stable_url(url)))
        if canonical:
            return canonical
        snapshot = await self.queue(url).get(**RPC)
        row = snapshot.to_dict()
        return await self.store.lesson(row["lesson_id"]) if row and row.get("lesson_id") else None

    async def start(self, day):
        run_id = identifier("chosun-daily:" + str(day))
        ref = self.store.collection("runs").document(run_id)
        async def operation(tx):
            row = (await ref.get(transaction=tx, **RPC)).to_dict()
            if row and row["status"] in {"completed", "partial", "failed"}:
                return None
            tx.set(ref, {"id": run_id, "scheduled_date": str(day), "status": "running",
                "started_at": datetime.now(timezone.utc), "finished_at": None,
                "attempts": row["attempts"] + 1 if row else 1, "stats": {}, "error_code": None})
            return run_id
        return await self.transaction(operation)

    async def enqueue(self, urls):
        processed = 0
        for url in dict.fromkeys(stable_url(url) for url in urls):
            async def operation(tx):
                ref = self.queue(url)
                row = (await ref.get(transaction=tx, **RPC)).to_dict()
                if row:
                    return row["state"] == "succeeded"
                tx.set(ref, {"url": url, "state": "pending", "discovered_at": datetime.now(timezone.utc),
                    "last_attempt_at": datetime(1970, 1, 1, tzinfo=timezone.utc), "attempts": 0,
                    "error_code": None, "lesson_id": None})
                return False
            processed += await self.transaction(operation)
        return processed

    def backlog(self):
        return self.store.collection("queue").where(filter=FieldFilter("state", "in", ["pending", "failed", "processing"]))

    async def remaining(self):
        await self.check_connection()
        count = await self.backlog().count().get(**RPC)
        return count[0][0].value

    async def pending(self, limit, preferred_urls=()):
        await self.check_connection()
        selected = []
        for url in dict.fromkeys(stable_url(url) for url in preferred_urls):
            row = (await self.queue(url).get(**RPC)).to_dict()
            if row and row["state"] != "succeeded":
                selected.append({"url": url})
                if len(selected) == limit:
                    return selected
        query = self.backlog().order_by("last_attempt_at").order_by("discovered_at").limit(limit)
        async for snapshot in query.stream(**RPC):
            item = {"url": snapshot.to_dict()["url"]}
            if item not in selected:
                selected.append(item)
            if len(selected) == limit:
                break
        return selected

    async def update_queue(self, url, values):
        async def operation(tx):
            tx.update(self.queue(url), values)
        await self.transaction(operation)

    async def attempt(self, url):
        await self.update_queue(url, {"state": "processing", "attempts": firestore.Increment(1), "last_attempt_at": datetime.now(timezone.utc)})

    async def save(self, url, result):
        canonical = stable_url(str(result.article.url))
        lesson_id = identifier(canonical)
        ref = self.store.collection("lessons").document(lesson_id)
        payload = result.model_dump(mode="json")
        # JSON preserves nested arrays (e.g. bounding polygons), unsupported as
        # native Firestore arrays of arrays. Exempt it from field indexing.
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 800_000:
            raise ArticleFetchError("result_too_large", "OCR result exceeds the Firestore storage limit.", 413)
        async def operation(tx):
            existing = await ref.get(transaction=tx, **RPC)
            if not existing.exists:
                now = datetime.now(timezone.utc)
                tx.set(ref, {"id": lesson_id, "canonical_url": canonical, "payload_json": encoded,
                    "article": payload["article"], "processing_status": payload["processing"]["status"],
                    "created_at": now, "updated_at": now})
            tx.update(self.queue(url), {"state": "succeeded", "error_code": None, "lesson_id": lesson_id})
            return not existing.exists
        return await self.transaction(operation)

    async def complete(self, url, lesson_id):
        await self.update_queue(url, {"state": "succeeded", "error_code": None, "lesson_id": lesson_id})

    async def failed(self, url, code):
        # A scheduled loser must not overwrite a simultaneous successful manual save.
        async def operation(tx):
            ref = self.queue(url)
            row = (await ref.get(transaction=tx, **RPC)).to_dict()
            if row and row["state"] != "succeeded":
                tx.update(ref, {"state": "failed", "error_code": code})
        await self.transaction(operation)

    async def finish(self, run_id, state, stats, code=None):
        async def operation(tx):
            tx.update(self.store.collection("runs").document(run_id), {
                "status": state, "stats": stats, "error_code": code, "finished_at": datetime.now(timezone.utc)})
        await self.transaction(operation)
