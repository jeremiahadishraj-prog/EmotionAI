import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    main._hits.clear()
    main._day.update({"stamp": main.time.strftime("%Y-%m-%d"), "count": 0})
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("RATE_PER_MINUTE", "20")
    for v in ("CHAT_MODEL", "QUICK_MODEL"):
        monkeypatch.delenv(v, raising=False)
    yield


def test_health_reports_configuration(monkeypatch):
    assert client.get("/api/health").json() == {"status": "ok", "configured": True, "provider": "groq"}
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert client.get("/api/health").json()["configured"] is False


def test_unknown_provider_falls_back_to_groq(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    assert client.get("/api/health").json()["provider"] == "groq"


def test_missing_key_gives_503_without_leaking(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    r = client.post("/api/chat", json={"prompt": "hi"})
    assert r.status_code == 503 and "test-key" not in r.text


@pytest.mark.parametrize("body", [{}, {"prompt": ""}, {"prompt": "x" * 12001}, {"prompt": "hi", "tier": "huge"},
                                   {"prompt": "hi", "images": ["a", "b", "c", "d"]},
                                   {"prompt": "hi", "images": ["a" * 900001]}])
def test_validation_rejects_bad_requests(body):
    assert client.post("/api/chat", json=body).status_code == 422


def test_groq_payload_and_models(monkeypatch):
    seen = {}

    async def fake(payload, key):
        seen["payload"], seen["key"] = payload, key
        return "Hello there"

    monkeypatch.setattr(main, "upstream_json", fake)
    r = client.post("/api/chat", json={"prompt": "hi"})
    assert r.status_code == 200 and r.json() == {"text": "Hello there"}
    assert seen["key"] == "test-key"
    assert seen["payload"]["model"] == "llama-3.3-70b-versatile"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    client.post("/api/chat", json={"prompt": "hi", "tier": "quick"})
    assert seen["payload"]["model"] == "llama-3.1-8b-instant"
    monkeypatch.setenv("CHAT_MODEL", "my-model")
    client.post("/api/chat", json={"prompt": "hi"})
    assert seen["payload"]["model"] == "my-model"


def test_groq_rejects_images():
    r = client.post("/api/chat", json={"prompt": "hi", "images": ["QUJD"]})
    assert r.status_code == 400


def test_anthropic_provider_payload(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    seen = {}

    async def fake(payload, key):
        seen["p"], seen["k"] = payload, key
        return "ok"

    monkeypatch.setattr(main, "upstream_json", fake)
    assert client.post("/api/chat", json={"prompt": "hi", "images": ["QUJD"]}).status_code == 200
    assert seen["k"] == "k2" and seen["p"]["model"] == "claude-sonnet-5-5"
    assert seen["p"]["messages"][0]["content"][0]["type"] == "image"


def test_groq_stream_line_normalisation():
    ev = main.groq_line_to_event('data: {"choices":[{"delta":{"content":"Hi"}}]}')
    assert json.loads(ev.decode()[5:]) == {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
    assert main.groq_line_to_event("data: [DONE]") is None
    assert main.groq_line_to_event('data: {"choices":[{"delta":{}}]}') is None
    assert main.groq_line_to_event(": keep-alive") is None
    assert b'"type":"error"' in main.groq_line_to_event('data: {"error":{"message":"x"}}')


def test_streaming_passthrough(monkeypatch):
    async def fake_stream(payload, key):
        assert payload["stream"] is True
        yield b'data: {"type":"content_block_delta","delta":{"text":"Hi"}}\n\n'

    monkeypatch.setattr(main, "upstream_stream", fake_stream)
    r = client.post("/api/chat", json={"prompt": "hi", "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream") and '"text":"Hi"' in r.text


def test_upstream_error_mapping():
    assert main._error_for(429).status_code == 429
    assert main._error_for(401).status_code == 502 and "rejected" in main._error_for(401).detail
    assert "model" in main._error_for(404).detail


def test_rate_limit(monkeypatch):
    async def fake(payload, key):
        return "ok"

    monkeypatch.setattr(main, "upstream_json", fake)
    monkeypatch.setenv("RATE_PER_MINUTE", "3")
    assert [client.post("/api/chat", json={"prompt": "hi"}).status_code for _ in range(5)] == [200, 200, 200, 429, 429]


def test_cors_allows_local_live_server(monkeypatch):
    r = client.options("/api/chat", headers={"Origin": "http://127.0.0.1:5500", "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200 and "access-control-allow-origin" in r.headers
