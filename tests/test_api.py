from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ScrapeResponse
from app.services import scrape as scrape_service

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_valid_scrape_is_explicitly_not_implemented():
    response = client.post(
        "/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"}
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"article_url": None},
        {"article_url": "not-a-url"},
        {"article_url": "https:chosun.com"},
        {"article_url": "ftp://www.chosun.com/article/"},
        {"article_url": "https://example.com/article/"},
        {"article_url": "https://chosun.com.evil.example/article/"},
        {"article_url": "https://user:password@chosun.com/article/"},
    ],
)
def test_invalid_request_never_calls_service(payload, monkeypatch):
    service = AsyncMock()
    monkeypatch.setattr(scrape_service, "scrape_article", service)
    response = client.post("/api/v1/scrape", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["location"] == ["body", "article_url"]
    assert error["details"][0]["message"]
    service.assert_not_awaited()


def test_malformed_json_returns_structured_error():
    response = client.post(
        "/api/v1/scrape", content="{", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_route_delegates_to_service_and_serializes_contract(monkeypatch):
    result = ScrapeResponse(
        article={"url": "https://www.chosun.com/example/", "title": "Example"},
        lesson_image={"url": "https://example.com/lesson.png", "width": 800},
        ocr={"raw_text": "안녕하세요 Hello", "korean_text": "안녕하세요", "english_text": "Hello"},
    )
    service = AsyncMock(return_value=result)
    monkeypatch.setattr(scrape_service, "scrape_article", service)
    response = client.post(
        "/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"}
    )
    assert response.status_code == 200
    assert response.json() == result.model_dump(mode="json")
    service.assert_awaited_once()
    assert str(service.call_args.args[0]) == "https://www.chosun.com/example/"


def test_openapi_documents_success_and_error_contracts():
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/api/v1/scrape"]["post"]["responses"]
    for status, model in [("200", "ScrapeResponse"), ("422", "ErrorResponse"), ("501", "ErrorResponse")]:
        assert responses[status]["content"]["application/json"]["schema"]["$ref"].endswith(model)
