"""Request dependencies shared by manual processing and saved-result routes."""

from fastapi import Request

from app.services.storage import StorageUnavailable


def get_store(request: Request):
    service = getattr(request.app.state, "daily_runner", None)
    if service is None:
        raise StorageUnavailable()
    return service.store
