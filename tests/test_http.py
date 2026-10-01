import requests

from ingest import http


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code = content, status
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding)


def test_fetch_bytes_caches_binary_exactly(tmp_path, monkeypatch):
    pdf = b"%PDF-1.7\r\n\xe2\xe3\xcf\xd3 binary \x00\xff"
    calls = []
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(http, "REQUEST_DELAY", 0)
    monkeypatch.setattr(
        http.requests, "get", lambda url, **kw: calls.append(url) or FakeResponse(pdf)
    )
    assert http.fetch_bytes("https://example.test/a.pdf", suffix=".pdf") == pdf
    assert (
        http.fetch_bytes("https://example.test/a.pdf", suffix=".pdf") == pdf
    )  # cached
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.part"))


def test_offline_never_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        http.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(AssertionError)
    )
    assert http.fetch("https://example.test/x", offline=True) is None
    assert http.fetch_bytes("https://example.test/y", offline=True) is None


def test_a_failed_request_still_starts_the_delay(tmp_path, monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("dropped")

    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(http, "_last_request_at", 0.0)
    monkeypatch.setattr(http.requests, "get", boom)
    assert http.fetch("https://example.test/z") is None
    assert http._last_request_at > 0


def test_error_pages_are_neither_cached_nor_served(tmp_path, monkeypatch):
    bodies = [b"<h1>Puslapis nerastas</h1>", b"<h1>Posedis</h1>"]
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(http, "REQUEST_DELAY", 0)
    monkeypatch.setattr(
        http.requests, "get", lambda url, **kw: FakeResponse(bodies.pop(0))
    )

    def ok(body: str) -> bool:
        return "nerastas" not in body

    url = "https://example.test/page"
    assert http.fetch(url, suffix=".html", valid=ok) is None
    assert not list(tmp_path.iterdir())
    assert http.fetch(url, suffix=".html", valid=ok) == "<h1>Posedis</h1>"
    # an error page cached before validation existed is a miss, not an answer
    old = "https://example.test/old"
    http._store(http._cache_path(old, ".html"), b"<h1>Puslapis nerastas</h1>")
    assert http.fetch(old, suffix=".html", offline=True, valid=ok) is None
