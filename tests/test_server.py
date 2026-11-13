"""Tests of the MCP server against a fake local HTTP service. No network, no key, never the production API.
Retries, limits and document checks are tested once, in the ocr-software package; here: the tools, the error mapping and the files.
Run (venv with the dependencies of pyproject.toml and pytest): python -m pytest integrations/mcp -p no:cacheprovider"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
try:
    import ocr_software  # noqa: F401  installed from PyPI
except ImportError:  # in the monorepo the package lies next to this folder
    sys.path.insert(0, str(HERE.parent.parent / "python"))

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from ocr_software import client as client_module  # noqa: E402
from ocr_software_mcp import server  # noqa: E402

PDF = b"%PDF-1.4 fake"
ENV_NAMES = ("OCR_KEY", "OCR_BASE_URL", "OCR_ALLOWED_DIR", "OCR_OUT_DIR", "OCR_SOFTWARE_API_KEY", "OCR_SOFTWARE_BASE_URL",
             "OCR_SOFTWARE_ALLOWED_DIR")


class Fake(BaseHTTPRequestHandler):
    """Records every request; scripted[(method, path)] is a list of (status, headers, body) used in order, the last one repeats."""
    log = []
    scripted = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        path = self.path.split("?")[0]
        Fake.log.append({"method": self.command, "path": path, "query": self.path.partition("?")[2],
                         "auth": self.headers.get("Authorization"), "idem": self.headers.get("Idempotency-Key"), "body": body})
        queue = Fake.scripted.get((self.command, path), [])
        status, headers, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


def err(status, code, message="m", headers=None):
    return (status, headers or {}, {"error": {"code": code, "message": message}})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=lambda: httpd.serve_forever(0.02), daemon=True).start()
    Fake.log, Fake.scripted = [], {}
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCR_BASE_URL", "http://127.0.0.1:%d" % httpd.server_address[1])
    monkeypatch.setenv("OCR_KEY", "ocr_testkey")
    monkeypatch.setenv("OCR_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(client_module, "BACKOFF_BASE_S", 0)
    yield Fake
    httpd.shutdown()


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(PDF)
    return str(p)


def test_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"list_pipelines", "quote", "submit_job", "job_status", "get_result"}


def test_list_pipelines_needs_no_key(fake, monkeypatch):
    monkeypatch.delenv("OCR_KEY")
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, {"pipelines": [{"pipeline": "scan-ctc@2"}]})]
    assert server.list_pipelines()["pipelines"][0]["pipeline"] == "scan-ctc@2"
    assert fake.log[0]["auth"] is None


def test_quote_is_dry_run(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"dry_run": True, "pages": 3})]
    assert server.quote(doc, "scan-ctc@2")["pages"] == 3
    call = fake.log[0]
    assert "dry_run=1" in call["query"] and "pipeline=scan-ctc@2" in call["query"]
    assert call["auth"] == "Bearer ocr_testkey" and call["body"] == PDF


def test_submit_job(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]
    out = server.submit_job(doc, "scan-ctc@2", job_budget=5000, human_confirmed=True, formats="json,md")
    assert out["job_id"] == "j1"
    call = fake.log[0]
    assert "job_budget=5000" in call["query"] and "formats=json,md" in call["query"] and "dry_run" not in call["query"]
    assert "wait" not in call["query"] and call["idem"]


def test_submit_needs_confirmation_and_budget(fake, doc):
    with pytest.raises(ToolError, match="quote"):
        server.submit_job(doc, "p@1", job_budget=5000, human_confirmed=False)
    with pytest.raises(ToolError, match="job_budget"):
        server.submit_job(doc, "p@1", job_budget=0, human_confirmed=True)
    assert fake.log == []


def test_api_error_becomes_tool_error_with_code_and_is_not_retried(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(422, "budget_too_low", "minimum 100")]
    with pytest.raises(ToolError, match="422 budget_too_low: minimum 100"):
        server.submit_job(doc, "p@1", job_budget=1, human_confirmed=True)
    assert len(fake.log) == 1


def test_missing_key_tells_the_human(fake, doc, monkeypatch):
    monkeypatch.delenv("OCR_KEY")
    with pytest.raises(ToolError, match="OCR_KEY is not set"):
        server.quote(doc, "p@1")
    assert fake.log == []


def test_key_not_leaked_in_errors(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(401, "unauthorized", "Wrong key")]
    with pytest.raises(ToolError) as e:
        server.quote(doc, "p@1")
    assert "ocr_testkey" not in str(e.value) and len(fake.log) == 1


def test_locked_out_goes_back_to_human_and_short_errors_are_retried(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(429, "locked_out", "locked", {"Retry-After": "900"})]
    with pytest.raises(ToolError, match="ask the human"):
        server.quote(doc, "p@1")
    fake.log.clear()
    fake.scripted[("POST", "/v1/jobs")] = [err(503, "busy"), (202, {}, {"job_id": "j2"})]
    assert server.submit_job(doc, "p@1", 100, True)["job_id"] == "j2"
    assert len(fake.log) == 2 and fake.log[0]["idem"] == fake.log[1]["idem"]


def test_package_variable_names_work_too(fake, doc, monkeypatch):
    monkeypatch.setenv("OCR_SOFTWARE_API_KEY", "ocr_newname")
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, {"pages": 1})]
    server.quote(doc, "p@1")
    assert fake.log[0]["auth"] == "Bearer ocr_newname"


def test_job_status_long_poll_and_bad_id(fake):
    fake.scripted[("GET", "/v1/jobs/j1")] = [(200, {}, {"state": "done", "pages_failed": 0})]
    assert server.job_status("j1", wait_seconds=99)["state"] == "done"
    assert "wait=60" in fake.log[0]["query"]
    for bad in ("../x", "a?b", "a/b", "x" * 101):
        with pytest.raises(ToolError, match="invalid job_id"):
            server.job_status(bad)
    assert len(fake.log) == 1


def test_get_result_writes_file_and_never_returns_content(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {"Content-Type": "text/markdown"}, b"## Page 1\nhello")]
    out = server.get_result("j1", "md")
    assert out["path"].endswith("j1.md") and out["bytes"] == 15 and "format=md" in fake.log[0]["query"]
    assert open(out["path"], "rb").read() == b"## Page 1\nhello" and "hello" not in json.dumps(out)


def test_get_result_not_ready_and_bad_input(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [err(409, "not_ready")]
    with pytest.raises(ToolError, match="409 not_ready"):
        server.get_result("j1")
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ToolError, match="invalid job_id"):
            server.get_result(bad)
    with pytest.raises(ToolError, match="format"):
        server.get_result("j1", "pdf")


def test_result_stays_inside_out_dir(fake, tmp_path):
    fake.scripted[("GET", "/v1/jobs/j-1_a/result")] = [(200, {}, b"{}")]
    out = server.get_result("j-1_a")
    assert Path(out["path"]).parent == (tmp_path / "out").resolve()


def test_get_result_never_overwrites(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, b"one"), (200, {}, b"two"), (200, {}, b"three")]
    paths = [server.get_result("j1", "text")["path"] for _ in range(3)]
    assert [Path(p).name for p in paths] == ["j1.txt", "j1-1.txt", "j1-2.txt"]
    assert [Path(p).read_bytes() for p in paths] == [b"one", b"two", b"three"]


def test_document_checks_apply_here_too(fake, tmp_path, monkeypatch):
    bad = tmp_path / "secrets.txt"
    bad.write_bytes(b"password=1")
    with pytest.raises(ToolError, match="not a PDF, PNG, JPEG or TIFF"):
        server.quote(str(bad), "p@1")
    with pytest.raises(ToolError, match="not a file"):
        server.quote(str(tmp_path), "p@1")
    inside = tmp_path / "in"
    inside.mkdir()
    (tmp_path / "b.pdf").write_bytes(PDF)
    monkeypatch.setenv("OCR_ALLOWED_DIR", str(inside))
    with pytest.raises(ToolError, match="outside the allowed folder"):
        server.quote(str(inside / ".." / "b.pdf"), "p@1")
    assert fake.log == []


def test_initialize_reports_our_version_and_the_order_of_the_tools():
    """Customer test 12.11.2026: serverInfo said 1.30.0 (the SDK) and gave no instructions."""
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_version == server.__version__ == "0.1.0"
    assert "quote" in options.instructions and "human_confirmed" in options.instructions
