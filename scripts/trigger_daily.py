"""External wake-up client. No OCR dependencies or database credentials needed."""

from datetime import datetime, timezone
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def main():
    base = os.environ["SCRAPE_BASE_URL"].rstrip("/")
    token = os.environ["SCRAPE_TRIGGER_TOKEN"]
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ValueError("SCRAPE_BASE_URL must be the HTTPS service origin")
    opener = build_opener(NoRedirect())

    def request(path, post=False):
        for attempt in range(6):
            headers = {"Accept": "application/json"}
            if post:
                headers["Authorization"] = "Bearer " + token
            try:
                req = Request(base + path, data=b"" if post else None, headers=headers)
                with opener.open(req, timeout=60) as response:
                    payload = response.read(1_048_577)
                    if len(payload) > 1_048_576:
                        raise ValueError("Response exceeds wake-up client limit")
                    return json.loads(payload)
            except HTTPError as exc:
                if exc.code not in {502, 503, 504} or attempt == 5:
                    raise RuntimeError(f"Daily API returned HTTP {exc.code}") from None
            except (URLError, TimeoutError, ValueError):
                if attempt == 5:
                    raise RuntimeError("Daily API did not become available") from None
            time.sleep(min(20, 2 ** attempt))

    status = request("/api/v1/jobs/daily")
    if not status["enabled"]:
        print("Daily scraping is disabled on the server.")
        return
    zone = ZoneInfo(status["timezone"])
    local = datetime.now(timezone.utc).astimezone(zone)
    if (local.hour, local.minute) < (status["hour"], status["minute"]):
        raise RuntimeError("External wake-up precedes the configured server schedule; align the workflow cron")
    day = local.date().isoformat()
    result = request("/api/v1/jobs/daily/trigger", post=True)
    print("Daily trigger:", result["status"])
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = request("/api/v1/jobs/daily")
        run = status.get("last_run")
        if run and run["scheduled_date"] == day and run["status"] in {"completed", "partial", "failed"}:
            print("Daily result:", run["status"], "counts:", run["stats"])
            if run["status"] != "completed":
                raise RuntimeError("Daily job recorded failures; inspect the status endpoint")
            return
        if status.get("local_error_code"):
            raise RuntimeError("Daily job has a storage/runtime error; inspect server configuration")
        time.sleep(30)
    raise RuntimeError("Daily job did not finish within the external polling budget")


if __name__ == "__main__":
    main()
