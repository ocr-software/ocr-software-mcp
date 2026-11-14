"""Tests of the MCP server against a fake local HTTP service. No network, no key, never the production API.
Retries, limits and document checks are tested once, in the ocr-software package; here: the tools, the error mapping and the files.
Run (venv with the dependencies of pyproject.toml and pytest): python -m pytest integrations/mcp -p no:cacheprovider"""
import asyncio
import json
import time
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
from ocr_software.advice import FAIR_USE  # noqa: E402
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
    monkeypatch.setattr(server, "POLL_MIN_S", 0)
    for memory in (server._quotes, server._job_files, server._results):
        memory.clear()
    yield Fake
    httpd.shutdown()


def call(tool, *args, **kwargs):
    return asyncio.run(tool(*args, **kwargs))


def make_result(pages=1, columns=False, words_per_line=6, low=()):
    """A json result like the API's: width 1000, lines of words with box and confidence. low: (page, word number) read with 0.5."""
    out = []
    for page in range(1, pages + 1):
        lines, n = [], 0
        for row in range(20):
            for x_start, x_end in ((50, 450), (550, 950)) if columns else ((50, 950),):
                words = []
                for i in range(words_per_line):
                    n += 1
                    w = (x_end - x_start) / words_per_line
                    words.append({"text": "w%d_%d" % (page, n), "confidence": 0.5 if (page, n) in low else 0.99, "source": "ctc",
                                  "verified": False, "box": [x_start + i * w, row * 40, x_start + (i + 1) * w - 5, row * 40 + 30]})
                lines.append({"words": words})
        out.append({"page": page, "status": "done", "safe_ok": False, "width": 1000, "height": 1000, "lines": lines})
    return {"schema_version": 1, "pipeline": "scan-ctc@2", "not_delivered": [], "pages": out}


def job_state(state="done", pages=3, done=3, **extra):
    return (200, {}, {"job_id": "j1", "state": state, "pages": pages, "pages_done": done, **extra})


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(PDF)
    return str(p)




DRY = {"dry_run": True, "pipeline": "scan-ctc@2", "pages": 42, "price_per_page_millicents": 100, "price_millicents": 4200,
       "max_charge_millicents": 0, "free_pages_left": 1000, "pages_covered": 42}


def quoted(fake, doc, dry=DRY):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, dry)]
    quote_id = call(server.quote, doc, "scan-ctc@2")["quote_id"]
    fake.log.clear()
    return quote_id


def test_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"list_pipelines", "quote", "submit_job", "job_status", "get_result", "uncertain_words"}


def test_tool_descriptions_stay_short():
    """Every description is read by the model on every start: keep them small (Desktop feedback 13.11.2026)."""
    tools = asyncio.run(server.mcp.list_tools())
    assert sum(len(json.dumps(t.model_dump())) for t in tools) < 9000


CATALOG = {"pipelines": [{"pipeline": "scan-ctc@2", "description": "d", "formats": ["json", "pdf"], "features": ["word_boxes"],
                          "capabilities": {"json": True}, "hardware": {"min_vram_mib": 8192}, "measured": [{"claim": "speed"}],
                          "price_per_page_millicents": 100, "available": True, "orderable": True,
                          "switch": [{"when": "hard_pages", "pipeline": "scan-vlm-ctc@2"}]},
                         {"pipeline": "scan-vlm-ctc@1", "description": "p", "available": True, "orderable": False}],
           "formats": ["json"], "features": ["word_boxes"], "parameters": [{"name": "pipeline"}]}


def test_list_pipelines_is_short_without_key_and_detail_gives_everything(fake, monkeypatch):
    monkeypatch.delenv("OCR_KEY")
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, CATALOG)]
    short = call(server.list_pipelines)
    assert fake.log[0]["auth"] is None
    first, second = short["pipelines"]
    assert short["default_pipeline"] == "scan-ctc@2"
    assert first["pipeline"] == "scan-ctc@2" and "price EUR 0.001 per page" in first["summary"] and "the default (fastest and cheapest)" in first["summary"]
    assert "for hard pages (poor scans, doubtful words) use scan-vlm-ctc@2" in first["summary"]  # switch.when, explained
    assert not {"hardware", "measured", "capabilities", "available", "orderable", "description", "switch"} & set(first)
    assert second["orderable"] is False and "parameters" not in short
    assert call(server.list_pipelines, detail=True) == CATALOG


def test_quote_without_pipeline_uses_the_default_and_says_so(fake, doc):
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, CATALOG)]
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, DRY)]
    out = call(server.quote, doc)
    assert out["pipeline"] == "scan-ctc@2" and "pipeline=scan-ctc@2" in fake.log[1]["query"]
    assert out["say_to_human"].startswith("No pipeline was chosen, so the default is used") and "42 pages with scan-ctc@2" in out["say_to_human"]
    assert "No pipeline was chosen" not in call(server.quote, doc, "scan-ctc@2")["say_to_human"]


def test_quote_without_pipeline_and_none_available_is_a_clear_error(fake, doc):
    fake.scripted[("GET", "/v1/catalog")] = [(200, {}, {"pipelines": [{"pipeline": "x@1", "available": False, "factor": 1}]})]
    with pytest.raises(ToolError, match="no pipeline is available"):
        call(server.quote, doc)
    assert all(c["method"] == "GET" for c in fake.log)


def test_quote_is_a_dry_run_and_speaks_euros_not_millicents(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, DRY)]
    out = call(server.quote, doc, "scan-ctc@2")
    sent = fake.log[0]
    assert "dry_run=1" in sent["query"] and "pipeline=scan-ctc@2" in sent["query"]
    assert sent["auth"] == "Bearer ocr_testkey" and sent["body"] == PDF
    assert out["pages"] == 42 and out["quote_id"].startswith("q_")
    assert out["say_to_human"] == "42 pages with scan-ctc@2: you pay EUR 0.00 today. List price EUR 0.042 in total (EUR 0.001 per page), shown for information only. " + FAIR_USE
    assert "millicents" not in json.dumps(out) and "4200" not in json.dumps(out)


def test_price_sentence_for_every_case_and_both_answers_of_the_open_question():
    free = dict(DRY)
    assert server._price_sentence(free, show_list_price=False) == "42 pages with scan-ctc@2: you pay EUR 0.00 today."
    paid = dict(DRY, max_charge_millicents=4200, free_pages_left=0)
    assert server._price_sentence(paid) == "42 pages with scan-ctc@2: up to EUR 0.042 is charged (EUR 0.001 per page)."
    assert server._price_sentence(dict(DRY, pages=1, price_millicents=0, max_charge_millicents=0)) == \
        "1 page with scan-ctc@2: nothing is charged (this service has no prices set)."
    assert server._price_sentence(dict(paid, pages_covered=10)).endswith("Only 10 of 42 pages would run.")
    assert server._eur(150000) == "EUR 1.50" and server._eur(0) == "EUR 0.00" and server._eur(12345) == "EUR 0.1235"


def test_quote_warnings_come_as_plain_sentences(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, dict(DRY, pages_covered=10, warnings=[{"code": "free_limit", "message": "free pages end"}]))]
    out = call(server.quote, doc, "scan-ctc@2")
    assert out["warnings"] == ["free pages end"]
    assert out["say_to_human"].endswith(" free pages end") and "Only" not in out["say_to_human"] and out["say_to_human"].startswith("42 pages with scan-ctc@2")


def test_path_errors_name_the_real_problem(fake, tmp_path, monkeypatch):
    with pytest.raises(ToolError, match="file not found"):
        call(server.quote, str(tmp_path / "nope.pdf"), "p@1")
    with pytest.raises(ToolError, match="folder, not a document"):
        call(server.quote, str(tmp_path), "p@1")
    monkeypatch.setattr(server.os, "name", "nt")
    with pytest.raises(ToolError, match=r"Git Bash path.*C:/Users"):
        call(server.quote, "/c/Users/x/a.pdf", "p@1")
    assert fake.log == []


def test_a_relative_path_gets_the_hint_to_give_an_absolute_one(fake):
    with pytest.raises(ToolError, match=r"file not found: clean\.png\. Give an absolute path"):
        call(server.quote, "clean.png", "p@1")


def test_outside_the_allowed_folder_the_answer_is_the_same_for_every_path(fake, tmp_path, monkeypatch):
    """Fund T12: an existing file, a folder and a missing path outside must not be told apart."""
    (tmp_path / "ok").mkdir()
    (tmp_path / "secret.pdf").write_bytes(PDF)
    monkeypatch.setenv("OCR_ALLOWED_DIR", str(tmp_path / "ok"))
    answers = set()
    for outside in (tmp_path / "secret.pdf", tmp_path, tmp_path / "nope.pdf"):
        with pytest.raises(ToolError, match="outside the allowed folder") as e:
            call(server.quote, str(outside), "p@1")
        answers.add(str(e.value).replace(str(outside), "X"))
    assert len(answers) == 1
    with pytest.raises(ToolError, match="file not found"):  # inside: the real problem is named
        call(server.quote, str(tmp_path / "ok" / "nope.pdf"), "p@1")
    assert fake.log == []


def test_submit_binds_document_pipeline_and_price_to_the_quote(fake, doc):
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running", "pages": 42, "price_millicents": 4200})]
    out = call(server.submit_job, quote_id, human_confirmed=True)
    assert out == {"job_id": "j1", "state": "running", "pages": 42}
    sent = fake.log[0]
    assert "job_budget=4200" in sent["query"] and "pipeline=scan-ctc@2" in sent["query"] and "dry_run" not in sent["query"]
    assert sent["idem"] and "wait" not in sent["query"]
    with pytest.raises(ToolError, match="unknown or expired quote_id"):  # one quote, one job
        call(server.submit_job, quote_id, human_confirmed=True)


def test_submit_without_price_sends_no_budget(fake, doc):
    quote_id = quoted(fake, doc, dict(DRY, price_millicents=0, price_per_page_millicents=0))
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]
    call(server.submit_job, quote_id, human_confirmed=True)
    assert "job_budget" not in fake.log[0]["query"]


def test_submit_needs_a_quote_and_a_yes(fake, doc):
    with pytest.raises(ToolError, match="unknown or expired quote_id"):
        call(server.submit_job, "q_nope", human_confirmed=True)
    quote_id = quoted(fake, doc)
    with pytest.raises(ToolError, match="say_to_human"):
        call(server.submit_job, quote_id)
    assert fake.log == []
    server._quotes[quote_id]["time"] -= server.QUOTE_TTL_S + 1
    with pytest.raises(ToolError, match="expired"):
        call(server.submit_job, quote_id, human_confirmed=True)


def test_submit_refuses_a_file_that_changed_since_the_quote(fake, doc):
    quote_id = quoted(fake, doc)
    open(doc, "wb").write(PDF + b"more pages")
    with pytest.raises(ToolError, match="changed since the quote"):
        call(server.submit_job, quote_id, human_confirmed=True)
    assert fake.log == []


def test_api_error_becomes_tool_error_with_code_and_is_not_retried(fake, doc):
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [err(422, "budget_too_low", "minimum 100")]
    with pytest.raises(ToolError, match="422 budget_too_low: minimum 100"):
        call(server.submit_job, quote_id, human_confirmed=True)
    assert len(fake.log) == 1


def test_missing_key_tells_the_human_where_the_key_belongs(fake, doc, monkeypatch):
    monkeypatch.delenv("OCR_KEY")
    with pytest.raises(ToolError, match="env field of this MCP server"):
        call(server.quote, doc, "p@1")
    assert fake.log == []


def test_key_not_leaked_in_errors(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(401, "unauthorized", "Wrong key")]
    with pytest.raises(ToolError) as e:
        call(server.quote, doc, "p@1")
    assert "ocr_testkey" not in str(e.value) and len(fake.log) == 1


def test_locked_out_goes_back_to_human_and_short_errors_are_retried(fake, doc):
    fake.scripted[("POST", "/v1/jobs")] = [err(429, "locked_out", "locked", {"Retry-After": "900"})]
    with pytest.raises(ToolError, match="ask the human"):
        call(server.quote, doc, "p@1")
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [err(503, "busy"), (202, {}, {"job_id": "j2"})]
    assert call(server.submit_job, quote_id, human_confirmed=True)["job_id"] == "j2"
    assert len(fake.log) == 2 and fake.log[0]["idem"] == fake.log[1]["idem"]


def test_package_variable_names_work_too(fake, doc, monkeypatch):
    monkeypatch.setenv("OCR_SOFTWARE_API_KEY", "ocr_newname")
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, DRY)]
    call(server.quote, doc, "p@1")
    assert fake.log[0]["auth"] == "Bearer ocr_newname"


def test_document_checks_apply_here_too(fake, tmp_path, monkeypatch):
    bad = tmp_path / "secrets.txt"
    bad.write_bytes(b"password=1")
    with pytest.raises(ToolError, match="not a PDF, PNG, JPEG or TIFF"):
        call(server.quote, str(bad), "p@1")
    inside = tmp_path / "in"
    inside.mkdir()
    (tmp_path / "b.pdf").write_bytes(PDF)
    monkeypatch.setenv("OCR_ALLOWED_DIR", str(inside))
    with pytest.raises(ToolError, match="outside the allowed folder"):
        call(server.quote, str(inside / ".." / "b.pdf"), "p@1")
    assert fake.log == []


# ---- waiting ----

def test_job_status_waits_until_done_in_one_call_and_never_polls_faster_than_the_floor(fake, monkeypatch):
    monkeypatch.setattr(server, "POLL_MIN_S", 0.2)
    fake.scripted[("GET", "/v1/jobs/j1")] = [job_state("running", 42, 5), job_state("running", 42, 6), job_state("done", 42, 42)]
    started = time.monotonic()
    out = call(server.job_status, "j1", wait_seconds=30)
    assert out["state"] == "done" and len(fake.log) == 3 and time.monotonic() - started >= 0.4
    assert "wait=30" in fake.log[0]["query"]
    for bad in ("../x", "a?b", "a/b", "x" * 101):
        with pytest.raises(ToolError, match="invalid job_id"):
            call(server.job_status, bad)
    assert len(fake.log) == 3


def test_job_status_returns_running_when_the_time_is_over(fake):
    fake.scripted[("GET", "/v1/jobs/j1")] = [job_state("running", 42, 5)]
    assert call(server.job_status, "j1", wait_seconds=0)["state"] == "running"
    assert "wait=0" in fake.log[0]["query"] and len(fake.log) == 1


def test_the_wait_of_one_call_is_capped(fake, monkeypatch):
    fake.scripted[("GET", "/v1/jobs/j1")] = [job_state("running", 42, 5)]
    monkeypatch.setattr(server, "MAX_WAIT_S", 1)
    monkeypatch.setattr(server, "POLL_SLICE_S", 1)
    started = time.monotonic()
    assert call(server.job_status, "j1", wait_seconds=999)["state"] == "running"
    assert time.monotonic() - started < 5 and "wait=1" in fake.log[0]["query"]


def test_job_view_is_small_queue_only_while_waiting_and_problems_are_said_plainly():
    waiting = server._job_view({"job_id": "j", "state": "running", "pages": 9, "pages_done": 0, "charged_millicents": 0,
                                "queue": {"class": "free", "position": 2, "pages_ahead": 30, "estimated_wait_seconds": 12, "jobs_total": 3}})
    assert waiting["queue"] == {"position": 2, "pages_ahead": 30, "estimated_wait_seconds": 12} and "charged" not in waiting
    running = server._job_view({"job_id": "j", "state": "running", "pages": 9, "pages_done": 3, "queue": {"pages_ahead": 0, "position": 1}})
    assert "queue" not in running
    done = server._job_view({"job_id": "j", "state": "done", "pages": 9, "pages_done": 8, "pages_failed": 1, "charged_millicents": 800,
                             "stop": "job_budget"})
    assert done["charged"] == "EUR 0.008" and "1 page(s) failed" in done["problem"] and "stopped: job_budget" in done["problem"]


def run_in_session(coro_factory, **session_args):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(server.mcp, **session_args) as session:
            return await coro_factory(session)
    return asyncio.run(go())


def test_job_view_passes_the_warnings_of_the_job_on():
    view = server._job_view({"job_id": "j", "state": "running", "pages": 9, "warnings": [{"code": "job_budget", "message": "only 3 pages run"}]})
    assert view["warnings"] == ["only 3 pages run"]
    assert "warnings" not in server._job_view({"job_id": "j", "state": "running", "pages": 9})


def test_submit_waits_reports_progress_and_saves_the_result_in_one_call(fake, doc):
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running", "pages": 3})]
    fake.scripted[("GET", "/v1/jobs/j1")] = [job_state("running", 3, 1), job_state("running", 3, 2), job_state("done", 3, 3)]
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(3)).encode())] * 2
    seen = []

    async def progress(done, total, message):
        seen.append((done, total, message))

    async def go(session):
        return await session.call_tool("submit_job", {"quote_id": quote_id, "human_confirmed": True, "wait_seconds": 20, "format": "json"},
                                       progress_callback=progress)
    out = json.loads(run_in_session(go).content[0].text)
    assert out["state"] == "done" and out["format"] == "json" and Path(out["path"]).is_file() and out["check"]["pages"] == 3
    assert [s[0] for s in seen] == [1, 2] and seen[0][1] == 3 and "of 3 pages" in seen[0][2]
    assert sum(1 for c in fake.log if c["path"] == "/v1/jobs/j1") == 3


def test_the_client_can_ask_the_human_itself_when_human_confirmed_is_left_out(fake, doc):
    from mcp.types import ElicitResult
    asked = []

    def run(answer):
        quote_id = quoted(fake, doc)
        fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]

        async def ask(context, params):
            asked.append(params.message)
            return ElicitResult(action="accept", content={"confirm": answer})

        async def go(session):
            return await session.call_tool("submit_job", {"quote_id": quote_id})
        return run_in_session(go, elicitation_callback=ask)
    yes = run(True)
    assert not yes.isError and "you pay EUR 0.00" in asked[0] and asked[0].endswith("Start the job?")
    no = run(False)
    assert no.isError and "did not agree" in no.content[0].text
    assert not any(c["method"] == "POST" for c in fake.log)  # the log holds the "no" run: it started nothing


# ---- results ----

def test_get_result_writes_next_to_the_document_by_default_never_into_the_app_folder(fake, doc, monkeypatch, tmp_path):
    monkeypatch.delenv("OCR_OUT_DIR")
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]
    call(server.submit_job, quote_id, human_confirmed=True)
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    out = call(server.get_result, "j1")
    assert Path(out["path"]) == (tmp_path / "ocr-results" / "j1.json").resolve()


def test_get_result_of_an_unknown_job_goes_to_documents(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("OCR_OUT_DIR")
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Documents").mkdir()
    fake.scripted[("GET", "/v1/jobs/j9/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    out = call(server.get_result, "j9")
    assert Path(out["path"]) == (tmp_path / "Documents" / "ocr-results" / "j9.json").resolve()
    assert "did not start this job" in out["note"] and str(Path(out["path"]).parent) in out["note"]


def test_without_a_documents_folder_the_result_goes_to_the_home_folder_and_the_answer_says_where(fake, monkeypatch, tmp_path):
    monkeypatch.delenv("OCR_OUT_DIR")
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))  # no Documents here (OneDrive redirect)
    fake.scripted[("GET", "/v1/jobs/j9/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    out = call(server.get_result, "j9")
    assert Path(out["path"]) == (tmp_path / "ocr-results" / "j9.json").resolve() and str(tmp_path) in out["note"]
    assert "note" not in call(server.get_result, "j9", out_dir=str(tmp_path / "mine"))  # the agent chose the place: no note


def test_out_dir_belongs_to_the_human_first_and_stays_inside_the_allowed_folder(fake, tmp_path, monkeypatch):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    with pytest.raises(ToolError, match="set by the human"):  # OCR_OUT_DIR is set by the fixture
        call(server.get_result, "j1", out_dir=str(tmp_path / "x"))
    monkeypatch.delenv("OCR_OUT_DIR")
    monkeypatch.setenv("OCR_ALLOWED_DIR", str(tmp_path / "ok"))
    with pytest.raises(ToolError, match="outside the allowed folder"):
        call(server.get_result, "j1", out_dir=str(tmp_path / "elsewhere"))
    monkeypatch.delenv("OCR_ALLOWED_DIR")
    out = call(server.get_result, "j1", out_dir=str(tmp_path / "mine"))
    assert Path(out["path"]).parent == (tmp_path / "mine").resolve()


def test_get_result_formats_content_stays_out_of_the_answer_and_json_is_byte_identical(fake):
    raw = json.dumps(make_result(1), ensure_ascii=False).encode()
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, raw), (200, {}, b"## Page 1\nhello")]
    as_json = call(server.get_result, "j1", "json")
    assert Path(as_json["path"]).read_bytes() == raw
    md = call(server.get_result, "j1", "md")
    assert md["path"].endswith("j1.md") and md["bytes"] == 15 and "hello" not in json.dumps(md)
    assert any("format=md" in c["query"] for c in fake.log)


def test_get_result_pdf_posts_the_original(fake, doc):
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]
    call(server.submit_job, quote_id, human_confirmed=True)
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    fake.scripted[("POST", "/v1/jobs/j1/result")] = [(200, {}, b"%PDF-1.7 searchable")]
    out = call(server.get_result, "j1", "pdf")
    assert out["path"].endswith("j1.pdf") and Path(out["path"]).read_bytes() == b"%PDF-1.7 searchable"
    post = [c for c in fake.log if c["method"] == "POST" and c["path"] == "/v1/jobs/j1/result"][0]
    assert "format=pdf" in post["query"] and post["body"] == PDF
    server._job_files.clear()
    with pytest.raises(ToolError, match="needs the original document: pass source_path"):
        call(server.get_result, "j1", "pdf")
    assert call(server.get_result, "j1", "pdf", source_path=doc)["bytes"] == 19


def test_get_result_not_ready_and_bad_input(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [err(409, "not_ready")]
    with pytest.raises(ToolError, match="Call job_status with this job_id"):
        call(server.get_result, "j1")
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ToolError, match="invalid job_id"):
            call(server.get_result, bad)
    with pytest.raises(ToolError, match="format"):
        call(server.get_result, "j1", "docx")


def test_result_stays_inside_out_dir_and_never_overwrites(fake, tmp_path):
    fake.scripted[("GET", "/v1/jobs/j-1_a/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    paths = [call(server.get_result, "j-1_a")["path"] for _ in range(3)]
    assert [Path(p).name for p in paths] == ["j-1_a.json", "j-1_a-1.json", "j-1_a-2.json"]
    assert all(Path(p).parent == (tmp_path / "out").resolve() for p in paths)


def test_an_unwritable_result_folder_is_a_clear_error(fake, tmp_path, monkeypatch):
    (tmp_path / "blocked").write_text("a file where the folder should be")
    monkeypatch.setenv("OCR_OUT_DIR", str(tmp_path / "blocked"))
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    with pytest.raises(ToolError, match="cannot create the result folder"):
        call(server.get_result, "j1")


def test_check_counts_words_confidence_uncertain_words_and_flags_columns(fake):
    result = make_result(2, low=[(1, 3), (1, 9)])
    result["pages"][1] = make_result(2, columns=True)["pages"][1]
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(result).encode())]
    check = call(server.get_result, "j1")["check"]
    assert check["pages"] == 2 and check["words"] == 120 + 240 and check["uncertain_words"] == 2 and "uncertain_words lists" in check["check"]
    rows = {r["page"]: r for r in check["per_page"]}  # 2 pages: all listed
    assert rows[1]["uncertain_words"] == 2 and "note" not in rows[1] and rows[1]["words"] == 120
    assert "several columns" in rows[2]["note"] and rows[2]["mean_confidence"] == 0.99


def test_check_lists_only_pages_with_a_note_when_the_document_is_long():
    result = make_result(12)
    result["pages"][4]["status"] = "failed"
    result["pages"][4]["error"] = {"code": "bad_image", "message": "m"}
    result["pages"][4]["lines"] = []
    check = server._summary(result)
    assert [r["page"] for r in check["per_page"]] == [5] and check["per_page"][0]["note"] == "failed: bad_image"


def test_a_single_column_page_is_not_flagged_and_a_title_across_does_not_hide_two_columns():
    page = make_result(1)["pages"][0]
    assert not server._multi_column(server._words(page), 1000)
    two = make_result(1, columns=True)["pages"][0]
    assert server._multi_column(server._words(two), 1000)
    two["lines"].insert(0, {"words": [{"text": "T", "confidence": 1, "source": "ctc", "verified": False, "box": [50, 0, 950, 30]}]})
    assert server._multi_column(server._words(two), 1000)


def test_a_short_two_column_page_is_flagged_and_the_hint_names_no_pipeline_that_reads_columns(fake):
    page = make_result(1, columns=True, words_per_line=3)["pages"][0]
    page["lines"] = page["lines"][:6]  # 3 rows, 2 columns, 18 words: the form of the finding T2 (22 words)
    assert server._multi_column(server._words(page), 1000)
    note = server._summary({"pipeline": "scan-ctc@2", "pages": [page]})["per_page"][0]["note"]
    assert "scan-ctc@2 reads line by line" in note and "No pipeline of ours reads columns in order" in note
    one_line = make_result(1, columns=True)["pages"][0]
    one_line["lines"] = one_line["lines"][:2]  # one row with a wide gap (letterhead): no columns
    assert not server._multi_column(server._words(one_line), 1000)


GALLERY = HERE.parents[2] / "gold" / "ergebnisse" / "galerie" / "matrix-gpu-20261111"  # only in the monorepo, not in the GitHub mirror


@pytest.mark.skipif(not GALLERY.is_dir(), reason="gold results are not in this checkout")
def test_the_column_hint_comes_on_the_spread_only_not_on_tables_forms_or_head_boxes():
    """Review 14.11.2026: the 26 scan-ctc@2 pages of the gallery. smoke07 is a spread (two pages side by side); smoke05 and smoke09
    are tables, pd05 has a head box beside the title, pd15 is a form with a margin strip: no hint there (rather too rare than wrong)."""
    flagged = []
    for path in sorted(GALLERY.glob("*/scan-ctc-2.json")):
        for page in json.loads(path.read_text(encoding="utf-8"))["pages"]:
            if server._multi_column(server._words(page), page.get("width") or 0):
                flagged.append(path.parent.name)
    assert flagged == ["smoke07"]


def test_pages_and_search_read_a_part_of_the_result_without_a_file(fake, tmp_path):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(5)).encode())]
    part = call(server.get_result, "j1", pages="2-3")
    assert part["text"].startswith("## Page 2\nw2_1 w2_2") and "## Page 3" in part["text"] and "## Page 4" not in part["text"]
    assert not part["truncated"] and not (tmp_path / "out").exists()
    hits = call(server.get_result, "j1", search="W4_7 ")
    assert hits["total"] == 1 and hits["matches"][0]["page"] == 4 and "w4_7" in hits["matches"][0]["line"]
    assert len([c for c in fake.log if c["path"].endswith("/result")]) == 1  # fetched once, kept in memory
    for bad in ("0-", "a", "5-2"):
        with pytest.raises(ToolError, match="pages must look like"):
            call(server.get_result, "j1", pages=bad)


def test_pages_and_search_reject_empty_values_missing_pages_and_options_that_do_not_apply(fake, tmp_path):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(3)).encode())]
    for kwargs, match in (({"search": ""}, "search is empty"), ({"search": "  "}, "search is empty"), ({"pages": ""}, "pages is empty"),
                          ({"pages": "1", "search": "w1"}, "not both"), ({"pages": "1", "format": "hocr"}, "do not apply"),
                          ({"search": "w1", "format": "json"}, "do not apply"), ({"pages": "1", "out_dir": str(tmp_path)}, "do not apply"),
                          ({"pages": "99"}, "page 99 not in this job: it has 3 page"), ({"pages": "2-5"}, "page 4, 5 not in this job")):
        with pytest.raises(ToolError, match=match):
            call(server.get_result, "j1", **kwargs)
    assert fake.log == [] or all(c["method"] == "GET" for c in fake.log)
    assert not (tmp_path / "out").exists()  # nothing was written
    assert call(server.get_result, "j1", pages="3")["text"].startswith("## Page 3")


def test_uncertain_words_lists_the_least_sure_first_with_both_readings_and_boxes(fake):
    result = make_result(2, low=[(1, 3), (2, 5)])
    word = result["pages"][1]["lines"][1]["words"][0]
    word.update(flag="differs", ctc_text="Iznos", text="lznps", confidence=0.95)
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(result).encode())]
    out = call(server.uncertain_words, "j1")
    assert out["total"] == 3 and [w["confidence"] for w in out["words"]] == [0.5, 0.5, 0.95]
    flagged = out["words"][2]
    assert flagged["other_reading"] == "Iznos" and flagged["page"] == 2 and len(flagged["box"]) == 4
    assert out["page_size"] == [{"page": 1, "width": 1000, "height": 1000}, {"page": 2, "width": 1000, "height": 1000}]  # page is a number as in words
    only = call(server.uncertain_words, "j1", pages="1", limit=1)
    assert only["total"] == 1 and len(only["words"]) == 1 and [r["page"] for r in only["page_size"]] == [1]


def test_uncertain_words_checks_its_limits_and_the_pages(fake):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(2, low=[(1, 3)])).encode())]
    for bad in (0, -1, 101):
        with pytest.raises(ToolError, match="limit must be between 1 and 100"):
            call(server.uncertain_words, "j1", limit=bad)
    for bad in (-0.1, 1.5, float("nan")):
        with pytest.raises(ToolError, match="max_confidence must be between 0 and 1"):
            call(server.uncertain_words, "j1", max_confidence=bad)
    assert call(server.uncertain_words, "j1", max_confidence=0)["total"] == 0
    assert call(server.uncertain_words, "j1", limit=100)["total"] == 1
    with pytest.raises(ToolError, match="pages must look like"):  # an empty string is no "not set"
        call(server.uncertain_words, "j1", pages="")
    with pytest.raises(ToolError, match="page 7 not in this job: it has 2 page"):
        call(server.uncertain_words, "j1", pages="1,7")


def test_initialize_reports_our_version_and_the_order_of_the_tools():
    """Customer test 12.11.2026: serverInfo said 1.30.0 (the SDK) and gave no instructions."""
    options = server.mcp._mcp_server.create_initialization_options()
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert options.server_version == server.__version__ and f'version = "{server.__version__}"' in pyproject
    assert "quote" in options.instructions and "human_confirmed" in options.instructions and "say_to_human" in options.instructions


def test_out_dir_cannot_climb_out_of_the_allowed_folder_by_dotdot_or_symlink(fake, tmp_path, monkeypatch):
    fake.scripted[("GET", "/v1/jobs/j1/result")] = [(200, {}, json.dumps(make_result(1)).encode())]
    monkeypatch.delenv("OCR_OUT_DIR")
    allowed = tmp_path / "ok"
    (allowed / "docs").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    monkeypatch.setenv("OCR_ALLOWED_DIR", str(allowed))
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: allowed))
    for bad in (str(allowed / ".." / "secret"), "../secret", str(tmp_path / "ok-but-longer"), str(tmp_path)):
        with pytest.raises(ToolError, match="outside the allowed folder"):
            call(server.get_result, "j1", out_dir=bad)
    try:
        (allowed / "link").symlink_to(tmp_path / "secret", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass  # no symlink right on this machine
    else:
        with pytest.raises(ToolError, match="outside the allowed folder"):
            call(server.get_result, "j1", out_dir=str(allowed / "link"))
    assert not (tmp_path / "secret" / "j1.json").exists()
    out = call(server.get_result, "j1", out_dir=str(allowed / "docs"))
    assert Path(out["path"]).parent == (allowed / "docs").resolve()


def test_one_quote_id_starts_one_job_even_when_two_calls_run_at_once_and_a_failed_start_keeps_it(fake, doc):
    quote_id = quoted(fake, doc)
    fake.scripted[("POST", "/v1/jobs")] = [err(400, "bad_pipeline")]
    with pytest.raises(ToolError, match="bad_pipeline"):
        call(server.submit_job, quote_id, human_confirmed=True)
    assert quote_id in server._quotes  # nothing started: the yes of the human still counts
    fake.scripted[("POST", "/v1/jobs")] = [(202, {}, {"job_id": "j1", "state": "running"})]
    fake.log.clear()

    async def twice():
        return await asyncio.gather(server.submit_job(quote_id, human_confirmed=True), server.submit_job(quote_id, human_confirmed=True),
                                    return_exceptions=True)
    results = asyncio.run(twice())
    assert sum(isinstance(r, dict) for r in results) == 1 and sum(isinstance(r, ToolError) for r in results) == 1
    assert len([r for r in fake.log if r["method"] == "POST"]) == 1


def test_both_folder_names_set_the_ocr_software_name_wins_like_in_the_client(fake, tmp_path, monkeypatch):
    (tmp_path / "ok").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "ok" / "a.pdf").write_bytes(PDF)
    monkeypatch.setenv("OCR_SOFTWARE_ALLOWED_DIR", str(tmp_path / "ok"))
    monkeypatch.setenv("OCR_ALLOWED_DIR", str(tmp_path / "other"))
    assert server._allowed_dir() == str(tmp_path / "ok") == client_module.Client().allowed_dir
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, DRY)]
    call(server.quote, str(tmp_path / "ok" / "a.pdf"), "p@1")


def test_quote_warnings_go_to_stderr_as_lines_and_stay_in_the_result(fake, doc, capsys):
    fake.scripted[("POST", "/v1/jobs")] = [(200, {}, dict(DRY, warnings=[{"code": "dpi_ignored", "message": "dpi is ignored"}]))]
    out = call(server.quote, doc, "p@1", 300)
    assert out["warnings"] == ["dpi is ignored"]
    assert "ocr-software-mcp: warning dpi_ignored: dpi is ignored" in capsys.readouterr().err
