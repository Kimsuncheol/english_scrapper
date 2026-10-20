"""PostgreSQL persistence and session-backed cross-process job exclusion."""

from contextlib import asynccontextmanager
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.services.discovery import stable_url
from app.services.errors import ArticleFetchError

RUN_LOCK = 7281946101
SCHEMA_LOCK = 7281946102


class StorageUnavailable(ArticleFetchError):
    def __init__(self):
        super().__init__("storage_unavailable", "Persistent PostgreSQL storage is unavailable.", 503)


class PostgresStore:
    def __init__(self, url: str, schema: str = "public"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
            raise ValueError("Invalid database schema name")
        self.url, self.schema = url, schema
        self.ready = False

    async def connect(self):
        if not self.url:
            raise StorageUnavailable()
        conn = None
        try:
            options = {}
            if urlsplit(self.url).hostname not in {"localhost", "127.0.0.1", "::1"} and "sslmode" not in psycopg.conninfo.conninfo_to_dict(self.url):
                options["sslmode"] = "require"
            conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True,
                connect_timeout=5, row_factory=dict_row, **options)
            await conn.execute("SET statement_timeout = 5000")
            await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            return conn
        except (psycopg.Error, OSError) as exc:
            if conn is not None:
                await conn.close()
            raise StorageUnavailable() from exc

    async def initialize(self):
        if self.ready:
            return
        async with await self.connect() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                await conn.execute("""CREATE TABLE IF NOT EXISTS daily_runs (
                    id text PRIMARY KEY, scheduled_date date UNIQUE NOT NULL,
                    status text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(),
                    finished_at timestamptz, attempts integer NOT NULL DEFAULT 1,
                    stats jsonb NOT NULL DEFAULT '{}', error_code text)""")
                await conn.execute("""CREATE TABLE IF NOT EXISTS daily_articles (
                    url text PRIMARY KEY, state text NOT NULL DEFAULT 'pending',
                    discovered_at timestamptz NOT NULL DEFAULT now(),
                    last_attempt_at timestamptz, attempts integer NOT NULL DEFAULT 0,
                    error_code text, lesson_id text)""")
                await conn.execute("""CREATE TABLE IF NOT EXISTS saved_lessons (
                    id text PRIMARY KEY, canonical_url text UNIQUE NOT NULL,
                    payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now())""")
        self.ready = True

    @asynccontextmanager
    async def run_session(self):
        await self.initialize()
        async with await self.connect() as conn:
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (RUN_LOCK,))).fetchone()
            if not row["acquired"]:
                yield None
                return
            # A dedicated direct/session connection holds this lock across all
            # awaited network/OCR operations. Closing it releases the lock.
            yield RunSession(conn)

    @asynccontextmanager
    async def article_session(self, url):
        """Exclude overlapping manual/scheduled work for a normalized URL."""
        await self.initialize()
        async with await self.connect() as conn:
            row = await (await conn.execute(
                "SELECT pg_try_advisory_lock(7281946, hashtext(%s)) AS acquired", (url,)
            )).fetchone()
            if not row["acquired"]:
                raise ArticleFetchError("scrape_busy", "This article is being processed. Retry later.", 503)
            yield RunSession(conn)

    async def status(self):
        await self.initialize()
        async with await self.connect() as conn:
            last = await (await conn.execute("SELECT * FROM daily_runs ORDER BY started_at DESC LIMIT 1")).fetchone()
            failures = await (await conn.execute("SELECT url, attempts, error_code, last_attempt_at FROM daily_articles WHERE state = 'failed' ORDER BY last_attempt_at DESC LIMIT 20")).fetchall()
            return {"last_run": last, "failures": failures}

    async def list_lessons(self, limit=20, offset=0):
        await self.initialize()
        async with await self.connect() as conn:
            return await (await conn.execute("""SELECT id, canonical_url, payload->'article' AS article,
                payload->'processing'->>'status' AS processing_status, created_at, updated_at
                FROM saved_lessons ORDER BY created_at DESC, id LIMIT %s OFFSET %s""", (limit, offset))).fetchall()

    async def lesson(self, lesson_id):
        await self.initialize()
        async with await self.connect() as conn:
            return await (await conn.execute("SELECT * FROM saved_lessons WHERE id=%s", (lesson_id,))).fetchone()

    async def articles(self, limit=20, offset=0):
        await self.initialize()
        async with await self.connect() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                total = await (await conn.execute("SELECT count(*) AS total FROM saved_lessons")).fetchone()
                items = await (await conn.execute("""SELECT id, canonical_url, payload->'article' AS article,
                    payload->'processing'->>'status' AS processing_status, created_at, updated_at
                    FROM saved_lessons ORDER BY created_at DESC, id LIMIT %s OFFSET %s""", (limit, offset))).fetchall()
                return {"items": items, "total": total["total"], "limit": limit, "offset": offset}


class RunSession:
    def __init__(self, conn):
        self.conn = conn

    async def check_connection(self):
        await self.conn.execute("SELECT 1")

    async def cached(self, url):
        return await (await self.conn.execute("""SELECT saved_lessons.* FROM saved_lessons
            WHERE canonical_url=%s OR id=(SELECT lesson_id FROM daily_articles WHERE url=%s)
            LIMIT 1""", (url, url))).fetchone()

    async def start(self, day):
        run_id = str(uuid5(NAMESPACE_URL, "chosun-daily:" + str(day)))
        row = await (await self.conn.execute("SELECT status FROM daily_runs WHERE scheduled_date=%s", (day,))).fetchone()
        if row and row["status"] in {"completed", "partial", "failed"}:
            return None
        await self.conn.execute("""INSERT INTO daily_runs(id, scheduled_date, status) VALUES (%s,%s,'running')
            ON CONFLICT (scheduled_date) DO UPDATE SET status='running', started_at=now(),
            finished_at=NULL, attempts=daily_runs.attempts+1, stats='{}', error_code=NULL""", (run_id, day))
        return run_id

    async def enqueue(self, urls):
        processed = 0
        for url in dict.fromkeys(stable_url(url) for url in urls):
            await self.conn.execute("INSERT INTO daily_articles(url) VALUES (%s) ON CONFLICT DO NOTHING", (url,))
            row = await (await self.conn.execute("SELECT state FROM daily_articles WHERE url=%s", (url,))).fetchone()
            processed += row["state"] == "succeeded"
        return processed

    async def remaining(self):
        row = await (await self.conn.execute("SELECT count(*) AS count FROM daily_articles WHERE state <> 'succeeded'")).fetchone()
        return row["count"]

    async def pending(self, limit, preferred_urls=()):
        preferred = list(dict.fromkeys(stable_url(url) for url in preferred_urls))
        return await (await self.conn.execute("""SELECT url FROM daily_articles WHERE state <> 'succeeded'
            ORDER BY array_position(%s::text[], url) NULLS LAST,
                last_attempt_at NULLS FIRST, discovered_at, url LIMIT %s""", (preferred, limit))).fetchall()

    async def attempt(self, url):
        # This also checks that the original lock-holding connection is alive
        # before starting another expensive scrape.
        await self.conn.execute("UPDATE daily_articles SET state='processing', attempts=attempts+1, last_attempt_at=now() WHERE url=%s", (url,))

    async def save(self, url, result):
        canonical = stable_url(str(result.article.url))
        lesson_id = str(uuid5(NAMESPACE_URL, canonical))
        async with self.conn.transaction():
            row = await (await self.conn.execute("""INSERT INTO saved_lessons(id,canonical_url,payload) VALUES (%s,%s,%s)
                ON CONFLICT (canonical_url) DO NOTHING RETURNING id""", (lesson_id, canonical, Jsonb(result.model_dump(mode="json"))))).fetchone()
            await self.complete(url, lesson_id)
        return row is not None

    async def complete(self, url, lesson_id):
        await self.conn.execute("UPDATE daily_articles SET state='succeeded', error_code=NULL, lesson_id=%s WHERE url=%s", (lesson_id, url))

    async def failed(self, url, code):
        await self.conn.execute("UPDATE daily_articles SET state='failed', error_code=%s WHERE url=%s", (code, url))

    async def finish(self, run_id, state, stats, code=None):
        await self.conn.execute("UPDATE daily_runs SET status=%s, stats=%s, error_code=%s, finished_at=now() WHERE id=%s", (state, Jsonb(stats), code, run_id))
