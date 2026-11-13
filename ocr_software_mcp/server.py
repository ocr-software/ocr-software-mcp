"""MCP server (stdio) for the ocr-software.com API. Thin wrapper: every tool is a few calls of the ocr-software package.

Settings by environment only: OCR_KEY (never printed, never in a tool result), OCR_BASE_URL, OCR_OUT_DIR, OCR_ALLOWED_DIR.
The names OCR_SOFTWARE_API_KEY, OCR_SOFTWARE_BASE_URL and OCR_SOFTWARE_ALLOWED_DIR of the package work too.
Retries, limits and document checks live in ocr_software.client (one copy for the CLI and this server).
Spec of every path, field and error code: <base>/openapi.json and agents.md of the API.

Design goal: few rounds for a foreign agent. quote gives a quote_id, one submit_job call can submit, wait and save, job_status
really waits, get_result tells which pages and words to check. State (quotes, job to file) lives in memory only: a restart
of the server loses it, the agent then runs quote again.
"""
import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, ElicitationCapability
from ocr_software import Client, OcrError
from ocr_software.advice import default_pipeline, summary
from ocr_software.advice import eur as _eur
from ocr_software.advice import price_sentence as _price_sentence
from ocr_software.client import read_document
from pydantic import BaseModel, Field

from . import __version__

RESULT_EXT = {"json": "json", "text": "txt", "md": "md", "hocr": "hocr", "pdf": "pdf"}
CATALOG_KEYS = ("pipeline", "formats", "features", "preview_note", "unavailable_note")
QUOTE_TTL_S = 3600
MAX_WAIT_S = 300  # one tool call; clients cut calls off after a while, the agent calls again
POLL_SLICE_S = 55  # one long poll of the API (the API holds up to 60 s)
POLL_MIN_S = 1.0  # never poll one job faster than this, however many pages finish (integrationen-regeln.md: do not flood)
LOW_CONFIDENCE = 0.9  # below this a word is listed as uncertain; the real hard words of the gold set sit at 0.83
UNCERTAIN_FLAGS = ("differs", "low_conf", "no_ctc", "not_in_model")  # flags of scan-vlm-ctc, see pipelines.md
INLINE_CHARS = 30000  # most text get_result(pages=...) returns in the chat
MAX_REMEMBERED = 50

INSTRUCTIONS = ("Read PDFs and images with ocr-software.com. Order: list_pipelines (show it with the default, the human picks the pipeline; without a choice quote uses the default and says so), quote (nothing runs; "
                "show say_to_human to the human), after a yes submit_job(quote_id, human_confirmed=true, wait_seconds=45, format=...) "
                "(job_status if it is not done yet). Results go to files; get_result reads pages or searches without opening the file; "
                "uncertain_words lists words to check against the page image.")

mcp = FastMCP("ocr-software", instructions=INSTRUCTIONS)
mcp._mcp_server.version = __version__  # else initialize reports the version of the MCP SDK

_quotes = {}  # quote_id -> what the human was shown; one submit_job uses it up
_job_files = {}  # job_id -> document path (results go next to it; pdf needs the original)
_results = {}  # job_id -> (bytes, parsed) json result of a finished job


def _remember(store, key, value):
    store[key] = value
    while len(store) > MAX_REMEMBERED:
        del store[next(iter(store))]


def _tool_error(e):
    if e.code == "no_key":
        return ToolError("No API key: OCR_KEY (or OCR_SOFTWARE_API_KEY) is not set. Ask the human to put the API key (starts with ocr_) "
                         "into the env field of this MCP server in the client configuration, not into the shell of the agent.")
    if e.code == "not_ready":
        return ToolError("The job is still running. Call job_status with this job_id (it waits for you), then get_result.")
    return ToolError(str(e))


def _client():
    try:
        return Client()
    except OcrError as e:
        raise _tool_error(e)


async def _call(func, *args, **kwargs):
    """Run a blocking client call without blocking the server; an OcrError becomes a ToolError with the API error code."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except OcrError as e:
        raise _tool_error(e)


def _check_path(path):
    """Say what is wrong with a path before the client reports a generic 'not a file'."""
    if os.path.isdir(path):
        raise ToolError("this is a folder, not a document: %s. Give the path of one PDF, PNG, JPEG or TIFF file." % path)
    if not os.path.exists(path):
        hint = ""
        if os.name == "nt" and re.match(r"^/[A-Za-z]/", path):
            hint = " It looks like a Git Bash path; on Windows give C:/Users/... or C:\\Users\\..."
        raise ToolError("file not found: %s.%s" % (path, hint))


class _Confirm(BaseModel):
    confirm: bool = Field(description="Start the job")


async def _human_confirms(ctx, sentence):
    """True or False if the client can ask the human directly (MCP elicitation), None if it cannot."""
    if ctx is None:
        return None
    try:
        if not ctx.request_context.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability())):
            return None
        answer = await ctx.elicit(sentence + " Start the job?", _Confirm)
    except McpError:
        return None
    return answer.action == "accept" and answer.data.confirm


def _job_view(job):
    """The job state without what an agent does not need: charges only when there are some, the queue only while waiting."""
    view = {k: job[k] for k in ("job_id", "state", "pages", "pages_done", "stop") if job.get(k) not in (None, "")}
    for k in ("pages_failed", "pages_not_run"):
        if job.get(k):
            view[k] = job[k]
    if job.get("charged_millicents"):
        view["charged"] = _eur(job["charged_millicents"])
    queue = job.get("queue") or {}
    if job.get("state") == "running" and queue.get("pages_ahead"):
        view["queue"] = {k: queue[k] for k in ("position", "pages_ahead", "estimated_wait_seconds", "estimated_done_seconds") if k in queue}
    if job.get("state") == "done":
        problems = ["%d page(s) failed" % job["pages_failed"] if job.get("pages_failed") else "",
                    "%d page(s) not run" % job["pages_not_run"] if job.get("pages_not_run") else "",
                    "stopped: %s" % job["stop"] if job.get("stop") else ""]
        if any(problems):
            view["problem"] = "done, but not every page was read: " + ", ".join(p for p in problems if p) + ". Tell the human."
    return view


async def _wait(ctx, client, job_id, wait_seconds):
    """Hold until the job is done or wait_seconds are over; one long poll at a time, progress to the client on every change."""
    deadline = time.monotonic() + max(0, min(MAX_WAIT_S, int(wait_seconds)))
    while True:
        started = time.monotonic()
        job = await _call(client.status, job_id, int(min(POLL_SLICE_S, math.ceil(max(0.0, deadline - started)))))
        if job.get("state") == "done" or time.monotonic() >= deadline:
            return job
        if ctx is not None:
            try:
                await ctx.report_progress(job.get("pages_done", 0), job.get("pages"), "%s of %s pages read" % (job.get("pages_done", 0), job.get("pages")))
            except Exception:  # progress is a courtesy; a client that cannot take it must not fail the job
                pass
        await asyncio.sleep(max(0.0, POLL_MIN_S - (time.monotonic() - started)))


# ---- result files and the quick check ----

def _documents_dir():
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def _inside(path, folder):
    try:
        base = os.path.normcase(os.path.realpath(folder))  # Windows: c:\ and C:\ are one place
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)), base]) == base
    except ValueError:  # other drive on Windows
        return False


def _out_folder(job_id, requested):
    """OCR_OUT_DIR of the human wins. Else ocr-results next to the document, else in Documents (never the folder of the app).
    A relative OCR_OUT_DIR is relative to the same place. The agent may ask for out_dir only if the human set no OCR_OUT_DIR."""
    configured = os.environ.get("OCR_OUT_DIR")
    base = Path(_job_files[job_id]).resolve().parent if job_id in _job_files else _documents_dir()
    if requested:
        if configured:
            raise ToolError("OCR_OUT_DIR is set by the human; do not pass out_dir.")
        allowed = os.environ.get("OCR_ALLOWED_DIR") or os.environ.get("OCR_SOFTWARE_ALLOWED_DIR")
        folder = base / requested
        if allowed and not _inside(folder, allowed):
            raise ToolError("out_dir is outside the allowed folder.")
        return folder
    return base / (configured or "ocr-results")


def _write_new(folder, job_id, ext, data):
    """Write to <job_id>.<ext>, or <job_id>-1.<ext>, -2 ... if it exists: never overwrite a file."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ToolError("cannot create the result folder %s: %s. Pass out_dir or ask the human to set OCR_OUT_DIR." % (folder, e.strerror))
    for n in range(1000):
        path = (folder / ("%s%s.%s" % (job_id, "-%d" % n if n else "", ext))).resolve()
        try:
            with open(path, "xb") as f:
                f.write(data)
            return str(path)
        except FileExistsError:
            continue
        except OSError as e:
            raise ToolError("cannot write the result to %s: %s. Pass out_dir or ask the human to set OCR_OUT_DIR." % (folder, e.strerror))
    raise ToolError("too many result files for %s in %s" % (job_id, folder))


def _words(page):
    return [w for line in page.get("lines", []) for w in line["words"]]


def _uncertain(word):
    return word["confidence"] < LOW_CONFIDENCE or word.get("flag") in UNCERTAIN_FLAGS


def _multi_column(words, width):
    """Hint, not proof: a vertical band of 4 percent of the page width between 20 and 80 percent stays empty in (nearly) every
    line and enough words stand on both sides. scan-ctc reads such a page line by line across the columns."""
    boxes = [w["box"] for w in words if w.get("box")]
    if len(boxes) < 60 or not width:
        return False
    bins = 50
    cover = [0] * bins
    for x0, _, x1, _ in boxes:
        for b in range(max(0, int(x0 / width * bins)), min(bins - 1, int(x1 / width * bins)) + 1):
            cover[b] += 1
    free = [b for b in range(10, 41) if cover[b] <= len(boxes) * 0.03]
    for b in free:
        if b + 1 in free:
            left = sum(1 for box in boxes if box[2] <= (b + 1) * width / bins)
            right = sum(1 for box in boxes if box[0] >= (b + 1) * width / bins)
            if left >= len(boxes) * 0.25 and right >= len(boxes) * 0.25:
                return True
    return False


def _summary(result):
    """Words, mean confidence and uncertain words per page, and a note where something needs a look."""
    rows, total, conf_sum, uncertain = [], 0, 0.0, 0
    for page in result.get("pages", []):
        words = _words(page)
        unc = sum(1 for w in words if _uncertain(w))
        mean = sum(w["confidence"] for w in words) / len(words) if words else None
        notes = []
        if page.get("status") != "done":
            notes.append("%s%s" % (page.get("status"), ": " + page["error"]["code"] if page.get("error") else ""))
        elif not words:
            notes.append("no words found")
        if _multi_column(words, page.get("width") or max((w["box"][2] for w in words if w.get("box")), default=0)):
            notes.append("probably several columns: scan-ctc reads line by line across them, the order of the text may be mixed; "
                         "tell the human, another pipeline may read columns in order")
        notes += [w["code"] for w in page.get("warnings", [])]
        if mean is not None and mean < LOW_CONFIDENCE:
            notes.append("low mean confidence")
        total, conf_sum, uncertain = total + len(words), conf_sum + sum(w["confidence"] for w in words), uncertain + unc
        rows.append({"page": page["page"], "words": len(words), "mean_confidence": None if mean is None else round(mean, 3),
                     "uncertain_words": unc, **({"note": "; ".join(notes)} if notes else {})})
    out = {"pages": len(rows), "words": total, "mean_confidence": round(conf_sum / total, 3) if total else None, "uncertain_words": uncertain}
    out["per_page"] = rows if len(rows) <= 8 else [r for r in rows if "note" in r]
    if uncertain:
        out["check"] = ("uncertain_words lists them (confidence below %s or a differing reading); look at them in the page image "
                        "before relying on them." % LOW_CONFIDENCE)
    return out


def _page_numbers(spec):
    """'3', '1-5', '2,4,7-9' -> sorted page numbers."""
    numbers = set()
    for part in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d{1,6})(?:-(\d{1,6}))?", part)
        if not m or (m.group(2) and int(m.group(2)) < int(m.group(1))):
            raise ToolError("pages must look like 3 or 1-5 or 2,4,7-9")
        numbers.update(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
    return sorted(numbers)


def _page_text(page):
    return "\n".join(" ".join(w["text"] for w in line["words"]) for line in page.get("lines", []))


def _load_result(client, job_id):
    """(bytes, parsed) json result of a finished job, kept in memory: the check, pages, search and uncertain_words use it."""
    if job_id not in _results:
        raw = client.result(job_id, "json")
        try:
            _remember(_results, job_id, (raw, json.loads(raw)))
        except ValueError:
            raise OcrError("the result of %s is not JSON: check OCR_BASE_URL" % client.base_url, "bad_response")
    return _results[job_id]


def _deliver(client, job_id, fmt, out_dir, source_path):
    """Blocking: fetch the result, write it to a file, return path, size and the quick check."""
    raw, parsed = _load_result(client, job_id)
    if fmt == "pdf":
        source = source_path or _job_files.get(job_id)
        if not source:
            raise ToolError("format pdf needs the original document: pass source_path (the server does not know the file of this job).")
        _check_path(source)
        data = client.result(job_id, "pdf", source)
    elif fmt == "json":
        data = raw
    else:
        data = client.result(job_id, fmt)
    path = _write_new(_out_folder(job_id, out_dir), job_id, RESULT_EXT[fmt], data)
    return {"path": path, "bytes": len(data), "format": fmt, "check": _summary(parsed)}


async def _finish(client, job_id, fmt, out_dir, source_path=None):
    return await _call(_deliver, client, job_id, fmt, out_dir, source_path)


def _check_format(fmt):
    if fmt is not None and fmt not in RESULT_EXT:
        raise ToolError("format must be one of %s" % ", ".join(RESULT_EXT))


# ---- tools ----

@mcp.tool()
async def list_pipelines(detail: bool = False) -> dict:
    """List what the service offers (no key needed): per pipeline one line (summary: what it delivers, price per page in euros,
    when to choose another one), formats and features, and default_pipeline: the fastest and cheapest available one, used when no pipeline is named. Show the available ones
    to the human with the default and let the human pick; never choose a pipeline yourself. detail=true adds
    descriptions, capabilities, measurements and hardware (long, rarely needed)."""
    catalog = await _call(_client().catalog)
    if detail:
        return catalog
    default = default_pipeline(catalog.get("pipelines", []))
    pipelines = []
    for p in catalog.get("pipelines", []):
        row = {k: p[k] for k in CATALOG_KEYS if k in p}
        row["summary"] = summary(p, default)
        row.update({k: p[k] for k in ("available", "orderable") if p.get(k) is False})
        pipelines.append(row)
    return {"default_pipeline": default, "pipelines": pipelines}


@mcp.tool()
async def quote(file_path: str, pipeline: str | None = None, dpi: int | None = None) -> dict:
    """Dry run: nothing runs, nothing is billed. Returns quote_id, the page count and say_to_human, one sentence with the
    price: show it to the human word for word and get a yes before submit_job. pipeline is name@version from list_pipelines; if the
    human named none, leave it out: the default (fastest and cheapest available) is used and the sentence names it.
    The quote_id is for one document, one pipeline and one job."""
    _check_path(file_path)
    client = _client()
    chosen = not pipeline
    if chosen:
        pipeline = await _call(client.default_pipeline)
    q = await _call(client.quote, file_path, pipeline, dpi)
    digest = hashlib.sha256(await _call(read_document, file_path, client.allowed_dir)).hexdigest()
    quote_id = "q_" + secrets.token_hex(6)
    q.setdefault("pipeline", pipeline)
    sentence = _price_sentence(q)
    if chosen:
        sentence = "No pipeline was chosen, so the default is used: the fastest and cheapest available one. " + sentence
    _remember(_quotes, quote_id, {"path": file_path, "sha256": digest, "pipeline": pipeline, "dpi": dpi, "pages": q.get("pages", 0),
                                   "budget": q.get("price_millicents") or None, "sentence": sentence, "time": time.monotonic()})
    out = {"quote_id": quote_id, "pipeline": pipeline, "pages": q.get("pages"), "say_to_human": sentence}
    if q.get("warnings"):
        out["warnings"] = [w.get("message", w.get("code", "")) for w in q["warnings"]]
    return out


@mcp.tool()
async def submit_job(quote_id: str, human_confirmed: bool = False, wait_seconds: int = 0, format: str | None = None,
                     client_ref: str | None = None, out_dir: str | None = None, ctx: Context = None) -> dict:
    """Start the job of a quote (document, pipeline and price are bound to the quote_id; the budget is the quoted price).
    Set human_confirmed=true only after the human saw say_to_human and agreed; if the client can ask the human itself it asks
    when you leave it false. wait_seconds (0 to 300): hold until the job is done; format (json, text, md, hocr, pdf) then
    also saves the result like get_result. Without wait_seconds it returns at once: poll job_status, keep the job_id."""
    _check_format(format)
    entry = _quotes.get(quote_id)
    if not entry or time.monotonic() - entry["time"] > QUOTE_TTL_S:
        raise ToolError("unknown or expired quote_id (kept 1 hour, one job each, lost when the server restarts). Run quote again.")
    if not human_confirmed:
        answer = await _human_confirms(ctx, entry["sentence"])
        if answer is None:
            raise ToolError("Show say_to_human of the quote to the human and set human_confirmed only after a yes.")
        if not answer:
            raise ToolError("The human did not agree. Nothing was started.")
    entry = _quotes.pop(quote_id, None)  # taken before any await: two parallel calls with one quote_id start one job at most
    if entry is None:
        raise ToolError("this quote_id is already in use by another call. Run quote again if you need a new job.")
    try:
        client = _client()
        digest = hashlib.sha256(await _call(read_document, entry["path"], client.allowed_dir)).hexdigest()
        if digest != entry["sha256"]:
            raise ToolError("the file changed since the quote. Run quote again and show the human the new quote.")
        # the client sends an Idempotency-Key: its retries never create a second job or charge
        job = await _call(client.submit, entry["path"], entry["pipeline"], None, None, entry["dpi"], client_ref, entry["budget"])
    except BaseException:  # nothing was started: the human's yes stays valid for a retry
        _quotes.setdefault(quote_id, entry)
        raise
    _remember(_job_files, job.get("job_id"), entry["path"])
    if wait_seconds <= 0:
        return _job_view(job)
    job = await _wait(ctx, client, job["job_id"], wait_seconds)
    view = _job_view(job)
    if job.get("state") == "done" and format:
        view.update(await _finish(client, job["job_id"], format, out_dir))
    return view


@mcp.tool()
async def job_status(job_id: str, wait_seconds: int = 45, format: str | None = None, out_dir: str | None = None,
                     ctx: Context = None) -> dict:
    """State and counts of a job. Holds until the job is done or wait_seconds (0 to 300) are over, and reports progress
    meanwhile: one call is enough, do not poll in a loop; if state is still running call again. When state is done, check
    problem (pages failed or not run) and tell the human. format (json, text, md, hocr, pdf) saves the result when done."""
    _check_format(format)
    client = _client()
    job = await _wait(ctx, client, job_id, wait_seconds)
    view = _job_view(job)
    if job.get("state") == "done" and format:
        view.update(await _finish(client, job_id, format, out_dir))
    return view


@mcp.tool()
async def get_result(job_id: str, format: str = "json", pages: str | None = None, search: str | None = None,
                     out_dir: str | None = None, source_path: str | None = None) -> dict:
    """Result of a finished job. Default: write it to a file (format json, text, md, hocr or pdf) and return path, size and
    check: words, mean confidence and uncertain words per page, notes such as probably several columns. The content stays
    out of the chat. pages ('3', '1-5') returns the text of those pages here, search returns the matching lines with
    page numbers, neither writes a file. out_dir: folder for the file, default ocr-results next to the document. pdf (searchable PDF)
    needs the original: source_path, or the file of a job started by this server. Results expire after about 24 hours.
    In the json: confidence is 0 to 1, box is [x0,y0,x1,y1] in pixels of the page image (page width, height). verified (per word) is
    true only where a second reader confirmed it (scan-vlm-ctc@2); safe_ok (per page) is always false today. Judge by confidence."""
    _check_format(format)
    client = _client()
    if not (pages or search):
        return await _finish(client, job_id, format, out_dir, source_path)
    result = (await _call(_load_result, client, job_id))[1]
    if pages:
        wanted = _page_numbers(pages)
        text = "\n\n".join("## Page %d\n%s" % (p["page"], _page_text(p)) for p in result["pages"] if p["page"] in wanted)
        return {"text": text[:INLINE_CHARS], "truncated": len(text) > INLINE_CHARS}
    needle = search.lower()
    hits = [{"page": p["page"], "line": line} for p in result["pages"] for line in _page_text(p).split("\n") if needle in line.lower()]
    return {"matches": hits[:20], "total": len(hits)}


@mcp.tool()
async def uncertain_words(job_id: str, max_confidence: float = LOW_CONFIDENCE, limit: int = 20, pages: str | None = None) -> dict:
    """Words the OCR read with low confidence (below max_confidence, default 0.9: the words of the gold set that were really
    wrong sat at about 0.83) or flagged as differing readings (scan-vlm-ctc), least
    sure first: page, word, confidence, other_reading if there is a second one, and box [x0,y0,x1,y1] in pixels of the page
    image (page_size gives width and height). Check these against the image of the page, do not trust them blindly, and tell
    the human what you corrected. Needs a finished job."""
    client = _client()
    result = (await _call(_load_result, client, job_id))[1]
    wanted = set(_page_numbers(pages)) if pages else None
    found, sizes = [], {}
    for page in result["pages"]:
        if wanted is not None and page["page"] not in wanted:
            continue
        for w in _words(page):
            if w["confidence"] < max_confidence or w.get("flag") in UNCERTAIN_FLAGS:
                row = {"page": page["page"], "text": w["text"], "confidence": round(w["confidence"], 3)}
                if w.get("ctc_text") and w["ctc_text"] != w["text"]:
                    row["other_reading"] = w["ctc_text"]
                if w.get("box"):
                    row["box"] = [round(v) for v in w["box"]]
                found.append(row)
                sizes[page["page"]] = [page.get("width"), page.get("height")]
    found.sort(key=lambda r: r["confidence"])
    shown = found[:max(1, min(100, limit))]
    return {"total": len(found), "words": shown, "page_size": {p: sizes[p] for p in sorted({r["page"] for r in shown})}}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
