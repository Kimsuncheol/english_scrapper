"""Schedule configuration; no local-file storage fallback."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DailyConfig:
    enabled: bool = False
    timezone: str = "Asia/Seoul"
    hour: int = 6
    minute: int = 0
    database_url: str = ""
    trigger_token: str = ""
    max_articles: int = 5
    retry_attempts: int = 3
    retry_base_seconds: float = 2
    request_gap_seconds: float = 2

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError("SCRAPE_HOUR must be 0..23 and SCRAPE_MINUTE 0..59")
        if self.enabled and not self.database_url:
            raise ValueError("SCRAPE_ENABLED requires a persistent PostgreSQL DATABASE_URL")
        if self.database_url and urlsplit(self.database_url).scheme not in {"postgres", "postgresql"}:
            raise ValueError("DATABASE_URL must use PostgreSQL; local SQLite is not supported")
        if not 1 <= self.max_articles <= 20 or not 1 <= self.retry_attempts <= 5:
            raise ValueError("Daily article/retry limits are outside supported bounds")
        if not 0 <= self.retry_base_seconds <= 30 or not 0 <= self.request_gap_seconds <= 60:
            raise ValueError("Daily backoff/gap values are outside supported bounds")

    @classmethod
    def from_env(cls):
        flag = os.getenv("SCRAPE_ENABLED", "false").lower()
        if flag not in {"true", "false"}:
            raise ValueError("SCRAPE_ENABLED must be true or false")
        return cls(enabled=flag == "true", timezone=os.getenv("SCRAPE_TIMEZONE", "Asia/Seoul"),
            hour=int(os.getenv("SCRAPE_HOUR", "6")), minute=int(os.getenv("SCRAPE_MINUTE", "0")),
            database_url=os.getenv("DATABASE_URL", ""), trigger_token=os.getenv("SCRAPE_TRIGGER_TOKEN", ""),
            max_articles=int(os.getenv("SCRAPE_DAILY_MAX_ARTICLES", "5")),
            retry_attempts=int(os.getenv("SCRAPE_RETRY_ATTEMPTS", "3")),
            retry_base_seconds=float(os.getenv("SCRAPE_RETRY_BASE_SECONDS", "2")),
            request_gap_seconds=float(os.getenv("SCRAPE_REQUEST_GAP_SECONDS", "2")))

    def due_date(self, now: datetime):
        local = now.astimezone(ZoneInfo(self.timezone))
        return local.date() if (local.hour, local.minute) >= (self.hour, self.minute) else None

    def next_due(self, now: datetime):
        local = now.astimezone(ZoneInfo(self.timezone))
        candidate = datetime.combine(local.date(), time(self.hour, self.minute), ZoneInfo(self.timezone))
        if local >= candidate:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
