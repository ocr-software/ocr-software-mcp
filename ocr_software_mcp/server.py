"""MCP server (stdio) for the ocr-software.com API. Thin wrapper: every tool is a few calls of the ocr-software package.

Settings by environment only: OCR_KEY (never printed, never in a tool result), OCR_BASE_URL, OCR_OUT_DIR, OCR_ALLOWED_DIR.
The names OCR_SOFTWARE_API_KEY, OCR_SOFTWARE_BASE_URL and OCR_SOFTWARE_ALLOWED_DIR of the package work too and win if both are set
(one order everywhere: the OCR_SOFTWARE_ name first, then the short OCR_ name).
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
import sys
import time
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, ElicitationCapability
from ocr_software import Client, OcrError
from ocr_software.advice import default_pipeline, summary
from ocr_software.advice import eur as _eur
from ocr_software.advice import price_sentence as _price_sentence, warning_lines
from ocr_software.client import check_job_id, read_document
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

INSTRUCTIONS = ("Read PDFs and images with ocr-software.com. Order: list_pipelines (show it with the default; you are asked to let the human pick the pipeline; without a choice quote uses the default and says so), quote (nothing runs; "
                "show say_to_human to the human), after a real yes of the human submit_job(quote_id, human_confirmed=true, wait_seconds=45, format=...) "
                "(job_status if it is not done yet). Results go to files; get_result reads pages or searches without opening the file; "
                "uncertain_words lists words to check against the page image; cancel_job stops a job after a yes of the human.")

mcp = FastMCP("ocr-software", instructions=INSTRUCTIONS, log_level="ERROR")  # the SDK logs every request at INFO to stderr
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


def _allowed_dir():
    return os.environ.get("OCR_SOFTWARE_ALLOWED_DIR") or os.environ.get("OCR_ALLOWED_DIR")  # same order as Client


def _check_path(path):
    """Say what is wrong with a path before the client reports a generic 'not a file'. The allowed folder is checked first,
    so outside of it the answer is the same whether the path exists or not."""
    allowed = _allowed_dir()
    if allowed and not _inside(path, allowed):
        raise ToolError("file is outside the allowed folder: %s" % path)
    if os.path.isdir(path):
        raise ToolError("this is a folder, not a document: %s. Give the path of one PDF, PNG, JPEG or TIFF file." % path)
    if not os.path.exists(path):
        hint = ""
        if os.name == "nt" and re.match(r"^/[A-Za-z]/", path):
            hint = " It looks like a Git Bash path; on Windows give C:/Users/... or C:\\Users\\..."
        elif not os.path.isabs(path):
            hint = " Give an absolute path: a relative one is resolved against the working folder of the server, which you do not know."
        raise ToolError("file not found: %s.%s" % (path, hint))


class _Confirm(BaseModel):
    confirm: bool = Field(description="Yes")


async def _human_confirms(ctx, sentence, question="Start the job?"):
    """True or False if the client can ask the human directly (MCP elicitation), None if it cannot."""
    if ctx is None:
        return None
    try:
        if not ctx.request_context.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability())):
            return None
        answer = await ctx.elicit(sentence + " " + question, _Confirm)
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
    if job.get("warnings"):  # the service gives them with the accepted job and the status: why fewer pages run, what is ignored
        view["warnings"] = [w.get("message", w.get("code", "")) for w in job["warnings"]]
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
        allowed = _allowed_dir()
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


def _row_groups(items):
    """Group (box, ...) items into text rows: a new row starts when a box begins more than 0.6 box heights below the row's first."""
    height = sorted(i[0][3] - i[0][1] for i in items)[len(items) // 2] or 1
    rows, start = [], None
    for item in sorted(items, key=lambda i: i[0][1]):
        if start is None or item[0][1] - start > 0.6 * height:
            rows.append([])
            start = item[0][1]
        rows[-1].append(item)
    return rows


def _multi_column(words, width):
    """Hint, not proof, and rather too rare than wrong. A vertical band between 20 and 80 percent of the page width stays empty
    in (nearly) every row, enough words stand on both sides in at least two rows each, and one of two things holds:
    the gutter is wide (8 percent or more: a spread, or a clear column gap), or it is text in columns: most rows have words on
    both sides and few words are numbers (a table has narrow gaps and numbers, a form has a head box or a margin strip with
    one side in few rows). Checked against the 26 scan-ctc@2 pages of gold/ergebnisse/galerie/matrix-gpu-20261111: only smoke07.
    Pipelines read such a page line by line across the columns. From 12 words on, so short forms and invoice heads count too."""
    items = [(w["box"], w) for w in words if w.get("box")]
    if len(items) < 12 or not width:
        return False
    boxes = [i[0] for i in items]
    bins = 50
    cover = [0] * bins
    for x0, _, x1, _ in boxes:
        for b in range(max(0, int(x0 / width * bins)), min(bins - 1, int(x1 / width * bins)) + 1):
            cover[b] += 1
    free = [b for b in range(10, 41) if cover[b] <= len(boxes) * 0.03]
    rows = _row_groups(items)
    numbers = sum(1 for _, w in items if not any(c.isalpha() for c in w["text"])) / len(items)
    for start in free:
        if start - 1 in free:
            continue  # a gap is looked at once, from its first bin
        end = start
        while end + 1 in free:
            end += 1
        if end == start:
            continue
        x = (start + end + 1) / 2 * width / bins  # middle of the gap
        left = [i for i in items if i[0][2] <= x]
        right = [i for i in items if i[0][0] >= x]
        if min(len(left), len(right)) < len(items) * 0.25 or min(len(_row_groups(left)), len(_row_groups(right))) < 2:
            continue
        both = sum(1 for row in rows if any(i[0][2] <= x for i in row) and any(i[0][0] >= x for i in row)) / len(rows)
        if end - start + 1 >= 4 or (both >= 0.6 and numbers < 0.3):
            return True
    return False


def _summary(result):
    """Words, mean confidence and the number of uncertain words per page, and a note where something needs a look."""
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
            notes.append("probably several columns: %s reads line by line across them and the order of the text may be mixed. "
                         "No pipeline of ours reads columns in order today (all three mix them): tell the human, and use the word boxes "
                         "(box x0) to tell the columns apart" % (result.get("pipeline") or "the pipeline"))
        notes += [w["code"] for w in page.get("warnings", [])]
        if mean is not None and mean < LOW_CONFIDENCE:
            notes.append("low mean confidence")
        total, conf_sum, uncertain = total + len(words), conf_sum + sum(w["confidence"] for w in words), uncertain + unc
        rows.append({"page": page["page"], "words": len(words), "mean_confidence": None if mean is None else round(mean, 3),
                     "uncertain": unc, **({"note": "; ".join(notes)} if notes else {})})
    out = {"pages": len(rows), "words": total, "mean_confidence": round(conf_sum / total, 3) if total else None, "uncertain": uncertain}
    out["per_page"] = rows if len(rows) <= 8 else [r for r in rows if "note" in r]
    if uncertain:
        out["next_step"] = ("the uncertain_words tool lists them (confidence below %s or a differing reading); look at them in the page image "
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


def _wanted_pages(spec, result):
    """The page numbers of spec; one the job does not have is an error that names the page count (empty text would look like an empty page)."""
    wanted = _page_numbers(spec)
    have = {p["page"] for p in result["pages"]}
    missing = [n for n in wanted if n not in have]
    if missing:
        raise ToolError("page %s not in this job: it has %d page(s), numbered from 1." % (", ".join(map(str, missing)), len(have)))
    return wanted


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
    out = {"path": path, "bytes": len(data), "format": fmt, "check": _summary(parsed)}
    if job_id not in _job_files and not out_dir and not os.environ.get("OCR_OUT_DIR"):
        out["note"] = ("this server did not start this job (or it restarted), so the file is not next to the document: it is in %s. "
                       "Pass out_dir, or ask the human to set OCR_OUT_DIR." % Path(path).parent)
    return out


async def _finish(client, job_id, fmt, out_dir, source_path=None):
    return await _call(_deliver, client, job_id, fmt, out_dir, source_path)


def _check_format(fmt):
    if fmt is not None and fmt not in RESULT_EXT:
        raise ToolError("format must be one of %s" % ", ".join(RESULT_EXT))


# ---- tools ----

@mcp.tool()
async def list_pipelines(detail: Annotated[bool, Field(description="true adds descriptions, capabilities, measurements and hardware (long)")] = False) -> dict:
    """List what the service offers (no key needed): per pipeline one line (summary: what it delivers, price per page in euros,
    when to choose another one), formats and features, and default_pipeline: the fastest and cheapest available one, used when no pipeline is named. Show the available ones
    to the human with the default; you are asked to let the human pick and not to choose a pipeline yourself. detail=true adds
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
async def quote(file_path: Annotated[str, Field(description="absolute path of one PDF, PNG, JPEG or TIFF file")],
                pipeline: Annotated[str | None, Field(description="name@version from list_pipelines; leave out if the human named none")] = None,
                dpi: Annotated[int | None, Field(description="render resolution for PDF pages; leave out for the default")] = None) -> dict:
    """Dry run: nothing runs, nothing is billed. Returns quote_id, the page count and say_to_human, one sentence with the
    price: you are asked to show it to the human word for word and get a yes before submit_job. pipeline is name@version from list_pipelines; if the
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
    notes = [w.get("message", w.get("code", "")) for w in q.get("warnings") or []]  # the human must see them
    sentence = " ".join([_price_sentence(dict(q, pages_covered=q.get("pages", 0)) if notes else q)] + notes)  # a warning already says "other pages are not run"
    if chosen:
        sentence = "No pipeline was chosen, so the default is used: the fastest and cheapest available one. " + sentence
    _remember(_quotes, quote_id, {"path": file_path, "sha256": digest, "pipeline": pipeline, "dpi": dpi, "pages": q.get("pages", 0),
                                   "budget": q.get("price_millicents") or None, "sentence": sentence, "time": time.monotonic()})
    out = {"quote_id": quote_id, "pipeline": pipeline, "pages": q.get("pages"), "say_to_human": sentence}
    for line in warning_lines(q):
        print("ocr-software-mcp: " + line, file=sys.stderr)  # stdout is the protocol; stderr is the log of the client
    if q.get("warnings"):
        out["warnings"] = [w.get("message", w.get("code", "")) for w in q["warnings"]]
    return out


@mcp.tool()
async def submit_job(quote_id: Annotated[str, Field(description="from quote; valid for one job, one hour")],
                     human_confirmed: Annotated[bool, Field(description="true only after the human saw say_to_human and agreed")] = False,
                     wait_seconds: Annotated[int, Field(description="0 to 300: hold until the job is done; 0 returns at once")] = 0,
                     format: Annotated[str | None, Field(description="json, text, md, hocr or pdf: save the result when done (needs wait_seconds)")] = None,
                     client_ref: Annotated[str | None, Field(description="your own reference, kept with the job")] = None,
                     out_dir: Annotated[str | None, Field(description="folder for the result file; not allowed if the human set OCR_OUT_DIR")] = None, ctx: Context = None) -> dict:
    """Start the job of a quote (document, pipeline and price are bound to the quote_id; the budget is the quoted price).
    You are asked to set human_confirmed=true only after the human saw say_to_human and really agreed (the server cannot check it); if the client can ask the human itself it asks
    when you leave it false. With wait_seconds and format the result is saved like get_result. Without wait_seconds it returns at once: use job_status, keep the job_id."""
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
async def job_status(job_id: Annotated[str, Field(description="from submit_job")],
                     wait_seconds: Annotated[int, Field(description="0 to 300: hold until the job is done")] = 45,
                     format: Annotated[str | None, Field(description="json, text, md, hocr or pdf: save the result when done")] = None,
                     out_dir: Annotated[str | None, Field(description="folder for the result file; not allowed if the human set OCR_OUT_DIR")] = None, ctx: Context = None) -> dict:
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
async def get_result(job_id: Annotated[str, Field(description="from submit_job")],
                     format: Annotated[str | None, Field(description="json (default), text, md, hocr or pdf; file output only")] = None,
                     pages: Annotated[str | None, Field(description="like 3 or 1-5 or 2,4,7-9: return the text of these pages here, no file")] = None,
                     search: Annotated[str | None, Field(description="return the lines containing this text with page numbers, no file")] = None,
                     out_dir: Annotated[str | None, Field(description="folder for the file; not allowed if the human set OCR_OUT_DIR")] = None,
                     source_path: Annotated[str | None, Field(description="original document, needed for pdf after a restart of this server")] = None) -> dict:
    """Result of a finished job. Default: write it to a file (format json, text, md, hocr or pdf) and return path, size and
    check: words, mean confidence and uncertain words per page, notes such as probably several columns. The content stays
    out of the chat. pages ('3', '1-5') returns the text of those pages here, search returns the matching lines with
    page numbers, neither writes a file (give neither format, out_dir nor source_path then). The file goes to ocr-results next to the document by default. pdf is a searchable PDF.
    Results expire after about 24 hours.
    In the json: confidence is 0 to 1, box is [x0,y0,x1,y1] in pixels of the page image (page width, height). verified (per word) is
    true only where a second reader confirmed it (scan-vlm-ctc@2); safe_ok (per page) is always false today. Judge by confidence."""
    _check_format(format)
    for name, value in (("pages", pages), ("search", search)):
        if value is not None and not value.strip():
            raise ToolError("%s is empty: leave it out, or give %s." % (name, "pages like 3 or 1-5" if name == "pages" else "the text to look for"))
    if pages is None and search is None:
        return await _finish(_client(), job_id, format or "json", out_dir, source_path)
    if pages is not None and search is not None:
        raise ToolError("give pages or search, not both.")
    if format or out_dir or source_path:
        raise ToolError("format, out_dir and source_path do not apply to pages or search: they return text here and write no file. Leave them out.")
    client = _client()
    result = (await _call(_load_result, client, job_id))[1]
    if pages is not None:
        wanted = _wanted_pages(pages, result)
        text = "\n\n".join("## Page %d\n%s" % (p["page"], _page_text(p)) for p in result["pages"] if p["page"] in wanted)
        return {"text": text[:INLINE_CHARS], "truncated": len(text) > INLINE_CHARS}
    needle = search.lower()
    hits = [{"page": p["page"], "line": line} for p in result["pages"] for line in _page_text(p).split("\n") if needle in line.lower()]
    return {"matches": hits[:20], "total": len(hits)}


@mcp.tool()
async def cancel_job(job_id: Annotated[str, Field(description="from submit_job")],
                     human_confirmed: Annotated[bool, Field(description="true only after the human agreed to cancel")] = False, ctx: Context = None) -> dict:
    """Stop a running job, or delete the result of a finished one (DELETE /v1/jobs/{id}). You are asked to set human_confirmed=true only
    after the human agreed (the server cannot check it). Pages already read stay charged; check job_status afterwards."""
    check_job_id(job_id)
    if not human_confirmed:
        answer = await _human_confirms(ctx, "This stops job %s or deletes its result." % job_id, "Cancel it?")
        if answer is None:
            raise ToolError("Ask the human whether job %s may be cancelled and set human_confirmed only after a yes." % job_id)
        if not answer:
            raise ToolError("The human did not agree. Nothing was cancelled.")
    raw, _ = await _call(_client()._call, "DELETE", "/v1/jobs/" + job_id)  # 202 with a body while it runs, 204 without once it is finished
    _results.pop(job_id, None)
    if not raw:
        return {"job_id": job_id, "state": "deleted", "note": "the job was finished: its result is deleted."}
    try:
        state = json.loads(raw).get("state", "cancelling")
    except (ValueError, AttributeError):
        state = "cancelling"
    return {"job_id": job_id, "state": state, "note": "the job stops after the page in progress: call job_status to see it done."}


@mcp.tool()
async def uncertain_words(job_id: Annotated[str, Field(description="from submit_job")],
                          max_confidence: Annotated[float, Field(description="0 to 1: list words below this confidence")] = LOW_CONFIDENCE,
                          limit: Annotated[int, Field(description="1 to 100: most words returned")] = 20,
                          pages: Annotated[str | None, Field(description="like 3 or 1-5: only these pages")] = None) -> dict:
    """Words the OCR read with low confidence (below max_confidence, default 0.9: the words of the gold set that were really
    wrong sat at about 0.83) or flagged as differing readings (scan-vlm-ctc), least
    sure first: page, word, confidence, other_reading if there is a second one, and box [x0,y0,x1,y1] in pixels of the page
    image (page_size: width and height per page). Check these against the image of the page, do not trust them blindly, and tell
    the human what you corrected. limit 1 to 100, max_confidence 0 to 1. Needs a finished job."""
    if not 0 <= max_confidence <= 1:
        raise ToolError("max_confidence must be between 0 and 1 (confidence is 0 to 1).")
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100.")
    client = _client()
    result = (await _call(_load_result, client, job_id))[1]
    wanted = set(_wanted_pages(pages, result)) if pages is not None else None
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
    shown = found[:limit]
    return {"total": len(found), "words": shown,
            "page_size": [{"page": p, "width": sizes[p][0], "height": sizes[p][1]} for p in sorted({r["page"] for r in shown})]}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
