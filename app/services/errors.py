"""Application errors shared by services and HTTP exception handling."""


class ArticleFetchError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502, *, upstream_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.upstream_status = upstream_status


class OCRError(ArticleFetchError):
    """Image transcription failures use the application error envelope."""
