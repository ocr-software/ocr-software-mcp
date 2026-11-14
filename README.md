# OCR MCP server for ocr-software.com: let Claude, Cursor and other agents read PDFs and scans

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ocr-software/ocr-software-mcp/main/assets/banner-dark.svg">
  <img alt="OCR for AI agents: an agent calls OCR and gets text, word box and confidence back" src="https://raw.githubusercontent.com/ocr-software/ocr-software-mcp/main/assets/banner-light.svg" width="100%">
</picture>

Give your AI agent a way to read documents: this MCP server sends PDF, PNG, JPEG and TIFF files to the [ocr-software.com OCR API](https://ocr-software.com/) and writes the result (JSON with word boxes, text, Markdown or hOCR) to a file. The API is hosted in Helsinki, EU (Hetzner). It also works against the package on your own server, which takes the same calls: set `OCR_BASE_URL` to its address ([install guide](https://ocr-software.com/docs/install/)).

The server is small by design. It enforces the price side: no job without a quote, and the quoted price is the cap. The rest is a request to the agent: it is asked to let a person pick the pipeline, show the page count and price, and wait for a real yes. More on the idea and on what to expect: [OCR for AI agents (MCP)](https://ocr-software.com/integrations/mcp/).

## Tools

Six tools. The API itself is described in the [API guide](https://ocr-software.com/docs/api-guide/), the rules for agents in the [agent guide](https://ocr-software.com/docs/agents.md). The normal path is three calls: `list_pipelines`, `quote`, `submit_job` with `wait_seconds` and `format`.

| Tool | What it does |
|---|---|
| `list_pipelines` | Short list: per pipeline one summary line (what it delivers, price per page in euros, when to choose another one), formats and features, and `default_pipeline` (the fastest and cheapest available one, used by `quote` when no pipeline is named), from `GET /v1/catalog` (no key needed); `detail=true` gives the full catalog. The agent is asked to show the list to the human and never to pick a pipeline itself. |
| `quote` | Dry run of `POST /v1/jobs`. `pipeline` is optional: without it the default is used and `say_to_human` says so. Returns a `quote_id` and `say_to_human`, a plain sentence with the page count and the amounts in euros. While billing is off it says that you pay EUR 0.00 today, shows the list price for information only and names the fair-use page and the contact. Nothing runs, nothing is billed. The quote is bound to the file (hash), the pipeline, the dpi and the price, lives one hour in the memory of the server and is used up by one job. |
| `submit_job` | Starts the job from the `quote_id`; the budget comes from the quote, so the agent never handles it. The agent is asked to set `human_confirmed=true` only after the human saw `say_to_human` and answered yes; the server cannot tell a real answer from a set flag. If the client supports elicitation and the flag is left out, the client asks the human itself. `wait_seconds` (up to 300) waits for the end with progress messages, `format` then writes the result in the same call. Sends an Idempotency-Key, so a retry never creates a second job or a second charge. |
| `job_status` | Waits up to `wait_seconds` (default 45) until the job is done, with progress messages, in one call. `done` does not mean every page worked: it says `problem` if pages failed or did not run. The queue block shows only while the job waits. |
| `get_result` | Fetches `json`, `text`, `md`, `hocr` or `pdf` (searchable PDF, needs the original document), writes it to a file and returns the path, the size and a quick check: words, mean confidence, uncertain words and a note for pages that look multi-column (from 12 words on; no pipeline of ours reads columns in order yet) or failed. With `pages` ("2-4") or `search` it returns text or matching lines instead of a file; then `format`, `out_dir` and `source_path` are refused, an empty `pages` or `search` is an error, and a page the job does not have is an error naming its page count. `pdf` needs the original: after a restart of the server pass `source_path`. Results expire after about 24 hours. |
| `uncertain_words` | The words the OCR is least sure about: page, word, confidence, the other reading where the pipeline has two (`differs`), the box in pixels and the page size. The agent should check these against the image. `limit` is 1 to 100, `max_confidence` 0 to 1; `page_size` lists width and height per page. |
| `cancel_job` | Stops a running job or deletes the result of a finished one (`DELETE /v1/jobs/{id}`). The agent is asked to set `human_confirmed=true` only after the human agreed; if the client can ask the human itself it asks when the flag is left out. Pages already read stay charged. |

`verified` (per word) and `safe_ok` (per page) in the JSON result are flags of the OCR service, not of this server. `verified` is true when a second reader confirmed the word: `scan-vlm-ctc@2` sets it where the character reader read the same text (`flag` ok); the other pipelines have one reader and leave it false. `safe_ok` is always false today (not calibrated), which says nothing against the page. Judge a word by `confidence` (0 to 1) and `flag`. `box` is `[x0, y0, x1, y1]` in pixels of the page image, origin top left; the page's `width`, `height` and `dpi` say which image, and PDF points are pixels * 72 / dpi. `uncertain_words` lists words with `confidence` below 0.9 (the really wrong words of the gold set sat at about 0.83) or a differing second reading.

Price: the API counts in millicents (100,000 millicents = 1 EUR); this server speaks euros. Today nothing is charged: `quote` says "you pay EUR 0.00 today", shows the list price for information and names the fair-use rules (https://ocr-software.com/legal/fair-usage/) and info@ocr-software.com for more volume.

The job budget is internal: `quote` gives the price in the plain sentence, the server sends the quoted price as the cap, and the job stops before a page that would exceed it. If the operator set no prices, no cap is sent.

Retries follow the agent guide: 429, 500, 502, 503, 504 and network errors up to 5 times with growing waits. A `Retry-After` over 60 seconds goes back to the human at once. Other 4xx errors are never retried.

## Settings

Environment variables only.

| Variable | Meaning |
|---|---|
| `OCR_KEY` (or `OCR_SOFTWARE_API_KEY`) | API key (starts with `ocr_`). Required except for `list_pipelines`. It belongs only in the `env` field of this server in the config of your client, not in the shell of the agent. Never print it, never paste it into the chat. Get one at [ocr-software.com/order](https://ocr-software.com/order/). |
| `OCR_BASE_URL` | Default `https://api.ocr-software.com`. For a self-hosted service, for example `http://127.0.0.1:8080`. |
| `OCR_OUT_DIR` | Where `get_result` writes; if set, the agent cannot choose another folder. Default: the folder `ocr-results` next to the document, for a job this server did not start (a new session or a restart: the server keeps jobs, quotes and document paths in memory only) `~/Documents/ocr-results`, or `~/ocr-results` if there is no `Documents` folder (for example with an OneDrive redirect); the answer always gives the full `path` and, in that case, a `note` saying why. Never the folder of the app. A relative value is relative to that place. Without it the agent may pass `out_dir` (inside `OCR_ALLOWED_DIR` if set). An existing file is never overwritten (`j1-1.json` instead). |
| `OCR_ALLOWED_DIR` | Optional. `quote` and `submit_job` then read only files below this folder; symlinks and `..` do not escape. Outside it the answer is always "outside the allowed folder", whether the path exists or not. Give absolute paths. |

## What the server enforces, and what the agent is asked to do

The server enforces:

- No job without a `quote_id`; a file that changed since the quote is refused.
- The quoted price is sent as the job budget (`job_budget`): the job stops before a page that would exceed it. (A quote without a price, for example while billing is off, sends no budget.)
- Every document is checked locally before it is sent: PDF, PNG, JPEG or TIFF by its first bytes, a regular file, at most 100 MiB.
- Results go to files, not into the conversation, so large documents do not fill the context.
- The key is read from the environment and never appears in a tool result.

The agent is asked to (the server cannot check it):

- ask the human before a job and show `say_to_human` word for word,
- not choose a pipeline itself; without a choice `quote` takes the default pipeline and says so in `say_to_human`,
- set `human_confirmed=true` only after a real answer of the human. Asking every time through the client (elicitation) is planned, not built.

## Install

Python 3.10 or newer. The package `ocr-software-mcp` runs the server (it needs the `ocr-software` package, installed with it).

```
uvx ocr-software-mcp            # run without installing (uv)
pipx install ocr-software-mcp   # or install once; the command is then ocr-software-mcp
pip install ocr-software-mcp    # inside a venv
```

### Claude Desktop

Add this to `claude_desktop_config.json`, then restart. The file is in `~/Library/Application Support/Claude/` on macOS and in `%APPDATA%\Claude\` on Windows:

```json
{"mcpServers": {"ocr-software": {"command": "uvx", "args": ["ocr-software-mcp"],
                                 "env": {"OCR_KEY": "ocr_..."}}}}
```

With pipx use `"command": "ocr-software-mcp"` and no `args`.

On Windows, `uv` must be on the PATH that Claude Desktop sees (install it with `winget install astral-sh.uv`, then restart Claude Desktop); otherwise use `pipx` and `"command": "ocr-software-mcp"`.

### Claude Code

```
claude mcp add ocr-software --env OCR_KEY=ocr_... -- uvx ocr-software-mcp
```

### Cursor

Use the same JSON as for Claude Desktop in `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (all projects).

### Your own server

```
"env": {"OCR_KEY": "ocr_...", "OCR_BASE_URL": "http://127.0.0.1:8080"}
```

The names `OCR_SOFTWARE_API_KEY`, `OCR_SOFTWARE_BASE_URL` and `OCR_SOFTWARE_ALLOWED_DIR` of the `ocr-software` package and its CLI are read too; the `OCR_SOFTWARE_` names win if both are set (the same order in the Python client and the command line).

## Tests

A fake local service, no network and no key needed. In a fresh clone, install the package with its test extra (this brings `mcp` and `ocr-software`), then run pytest:

```
python -m venv .venv
.venv/bin/pip install -e ".[test]"            # Windows: .venv\Scripts\pip install -e ".[test]"
.venv/bin/python -m pytest -p no:cacheprovider .    # Windows: .venv\Scripts\python -m pytest -p no:cacheprovider .
```

## License

MIT, see `LICENSE`. More about the service, its pipelines and output formats: [ocr-software.com](https://ocr-software.com/).

## Provider and legal

The OCR runs on the [ocr-software.com](https://ocr-software.com/) API, operated by Helm & Nagel GmbH. [Terms](https://ocr-software.com/legal/saas-terms/) · [Privacy](https://ocr-software.com/legal/privacy/) · [Imprint](https://ocr-software.com/legal/imprint/) · [Security](https://ocr-software.com/legal/security/)

mcp-name: io.github.ocr-software/ocr-software-mcp
