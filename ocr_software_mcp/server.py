"""MCP server (stdio) for the ocr-software.com API. Thin wrapper: every tool is one API call of the ocr-software package.

Settings by environment only: OCR_KEY (never printed, never in a tool result), OCR_BASE_URL, OCR_OUT_DIR, OCR_ALLOWED_DIR.
The names OCR_SOFTWARE_API_KEY, OCR_SOFTWARE_BASE_URL and OCR_SOFTWARE_ALLOWED_DIR of the package work too.
Retries, limits and document checks live in ocr_software.client (one copy for the CLI and this server).
Spec of every path, field and error code: <base>/openapi.json and agents.md of the API.
"""
import contextlib
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from ocr_software import Client, OcrError

from . import __version__

RESULT_EXT = {"json": "json", "text": "txt", "md": "md", "hocr": "hocr"}

INSTRUCTIONS = ("Read PDFs and images with ocr-software.com. Order: list_pipelines (the human picks the pipeline), quote (page count, "
                "nothing runs), show the quote to the human, only after a yes submit_job with human_confirmed=true, then job_status, get_result.")

mcp = FastMCP("ocr-software", instructions=INSTRUCTIONS)
mcp._mcp_server.version = __version__  # else initialize reports the version of the MCP SDK


@contextlib.contextmanager
def _api():
    """Yield a client; an OcrError becomes a ToolError with the API error code."""
    try:
        yield Client()
    except OcrError as e:
        if e.code == "no_key":
            raise ToolError("OCR_KEY is not set. Ask the human to put the API key (starts with ocr_) into the MCP client configuration.")
        raise ToolError(str(e))


@mcp.tool()
def list_pipelines() -> dict:
    """List what the service offers (no key needed): pipelines (name@version, description, capabilities, available),
    formats, features. Show the available ones to the human and let the human pick; never choose a pipeline yourself."""
    with _api() as client:
        return client.catalog()


@mcp.tool()
def quote(file_path: str, pipeline: str, dpi: int | None = None) -> dict:
    """Dry run: nothing runs, nothing is billed. Returns the page count (and the price if the operator set prices) and warnings.
    Show it to the human and get a yes before submit_job. pipeline is name@version from list_pipelines."""
    with _api() as client:
        return client.quote(file_path, pipeline, dpi)


@mcp.tool()
def submit_job(file_path: str, pipeline: str, job_budget: int, human_confirmed: bool, formats: str | None = None,
               features: str | None = None, dpi: int | None = None, client_ref: str | None = None) -> dict:
    """Submit one document (PDF, PNG, JPEG, TIFF) for a real job. Only after the human saw the quote and agreed:
    set human_confirmed to true. job_budget is a whole number in millicents (1 EUR = 100000), the job stops before a page
    that would exceed it. formats (json,text,md,hocr) and features (word_boxes,table_cells,layout_classes,header_footer)
    are only a check at submit. Returns the job state; the job may still run: poll with job_status, keep the job_id."""
    if not human_confirmed:
        raise ToolError("Run quote first, show page count and price to the human and set human_confirmed only after a yes.")
    if job_budget < 1:
        raise ToolError("job_budget must be a whole number of millicents, at least one page price.")
    with _api() as client:  # the client sends an Idempotency-Key: its retries never create a second job or charge
        return client.submit(file_path, pipeline, formats, features, dpi, client_ref, job_budget)


@mcp.tool()
def job_status(job_id: str, wait_seconds: int = 30) -> dict:
    """State and counts of a job. Holds up to wait_seconds (0 to 60) until the job changes. Stop when state is done, then
    check pages_failed, pages_not_run and stop and tell the human; done does not mean every page worked."""
    with _api() as client:
        return client.status(job_id, wait_seconds)


@mcp.tool()
def get_result(job_id: str, format: str = "json") -> dict:
    """Fetch the result of a finished job (format json, text, md or hocr), write it to a file in OCR_OUT_DIR (default
    ./ocr-results) and return the path and size, never the content. Results expire after about 24 hours."""
    if format not in RESULT_EXT:
        raise ToolError("format must be one of %s" % ", ".join(RESULT_EXT))
    with _api() as client:
        data = client.result(job_id, format)
    out_dir = os.environ.get("OCR_OUT_DIR", "ocr-results")
    os.makedirs(out_dir, exist_ok=True)
    path = _write_new(out_dir, job_id, RESULT_EXT[format], data)
    return {"path": path, "bytes": len(data), "format": format}


def _write_new(out_dir, job_id, ext, data):
    """Write to <job_id>.<ext>, or <job_id>-1.<ext>, -2 ... if it exists: never overwrite a file."""
    for n in range(1000):
        path = os.path.abspath(os.path.join(out_dir, "%s%s.%s" % (job_id, "-%d" % n if n else "", ext)))
        try:
            with open(path, "xb") as f:
                f.write(data)
            return path
        except FileExistsError:
            continue
    raise ToolError("too many result files for %s in %s" % (job_id, out_dir))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
