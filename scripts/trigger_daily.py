"""External daily wake-up/recovery client; no OCR or database credentials."""

from datetime import datetime, timezone
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Never forward the trigger secret to another location.
        return None


class DailyClient:
    def __init__(self, base, token, *, opener=None, sleep=time.sleep,
                 monotonic=time.monotonic, budget_seconds=1800):
        self.base = base.rstrip("/")
        parts = urlsplit(self.base)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.path or parts.query or parts.fragment:
            raise ValueError("SCRAPE_BASE_URL must be the HTTPS service origin")
        if not token or not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("SCRAPE_TRIGGER_TOKEN must be a nonempty printable ASCII token without spaces")
        self.token = token
        self.opener = opener or build_opener(NoRedirect())
        self.sleep, self.monotonic = sleep, monotonic
        self.deadline = monotonic() + budget_seconds

    def remaining(self):
        left = self.deadline - self.monotonic()
        if left <= 0:
            raise RuntimeError("Daily job did not finish within the external wake-up/polling budget")
        return left

    def pause(self, seconds):
        self.sleep(min(seconds, self.remaining()))

    def request(self, path, post=False):
        for attempt in range(6):
            timeout = min(60, self.remaining())
            headers = {"Accept": "application/json"}
            if post:
                headers["Authorization"] = "Bearer " + self.token
            try:
                req = Request(self.base + path, data=b"" if post else None, headers=headers)
                with self.opener.open(req, timeout=timeout) as response:
                    payload = response.read(1_048_577)
                    if len(payload) > 1_048_576:
                        raise ValueError("Oversized API response")
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError("Invalid API response")
                    return value
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in {502, 503, 504} or attempt == 5:
                    raise RuntimeError(f"Daily API returned HTTP {status}") from None
            except (OSError, ValueError):
                if attempt == 5:
                    raise RuntimeError("Daily API did not become available") from None
            self.pause(min(20, 2 ** attempt))


def run_daily(client, *, now=None):
    now = now or datetime.now(timezone.utc)
    while True:
        status = client.request("/api/v1/jobs/status")
        if not status["enabled"]:
            raise RuntimeError("Daily scraping is disabled on the server; check SCRAPE_ENABLED")
        local = now.astimezone(ZoneInfo(status["timezone"]))
        if (local.hour, local.minute) < (status["hour"], status["minute"]):
            raise RuntimeError("External wake-up precedes the configured server schedule; align the workflow cron")
        run = status.get("last_run")
        if run and run["scheduled_date"] == local.date().isoformat() and run["status"] in {"completed", "partial", "failed"}:
            # Do not print URLs, response bodies, error strings, or credentials.
            print("Daily result:", run["status"])
            if run["status"] != "completed":
                raise RuntimeError("Daily job recorded failures; inspect the status endpoint")
            return

        # Re-submit safely while unfinished: an active process returns
        # already_running, whereas a replacement process resumes the durable run.
        # This removes any dependency on an in-process clock after restarts.
        result = client.request("/api/v1/jobs/daily/trigger", post=True)
        if result.get("status") not in {"accepted", "already_running", "already_completed", "cooldown"}:
            raise RuntimeError("Daily trigger was not accepted; check server schedule/configuration")
        client.pause(30)


def main():
    client = DailyClient(os.environ["SCRAPE_BASE_URL"], os.environ["SCRAPE_TRIGGER_TOKEN"])
    run_daily(client)


if __name__ == "__main__":
    main()
