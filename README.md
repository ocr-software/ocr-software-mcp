# OCR MCP server for ocr-software.com: let Claude, Cursor and other agents read PDFs and scans

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
  <img alt="OCR for AI agents: an agent calls OCR and gets text, word box and confidence back" src="assets/banner-light.svg" width="100%">
</picture>

Give your AI agent a way to read documents: this MCP server sends PDF, PNG, JPEG and TIFF files to the [ocr-software.com OCR API](https://ocr-software.com/) and writes the result (JSON with word boxes, text, Markdown or hOCR) to a file. The API is hosted in Helsinki, EU (Hetzner). It also works against the package on your own server, which takes the same calls: set `OCR_BASE_URL` to its address ([install guide](https://ocr-software.com/docs/install/)).

The server is small and strict by design. A person picks the pipeline, sees the page count first, and says yes before anything runs. More on the idea and on what to expect: [OCR for AI agents (MCP)](https://ocr-software.com/integrations/mcp/).

## Tools

Five tools, each one API call. The API itself is described in the [API guide](https://ocr-software.com/docs/api-guide/), the rules for agents in the [agent guide](https://ocr-software.com/docs/agents.md).

| Tool | What it does |
|---|---|
| `list_pipelines` | Lists pipelines, formats and features (`GET /v1/catalog`, no key needed). The human picks the pipeline, the agent never does. |
| `quote` | Dry run of `POST /v1/jobs`: returns the page count and warnings, and the price if the operator set prices. Nothing runs, nothing is billed. |
| `submit_job` | Starts a real job. Needs `pipeline`, `job_budget` and `human_confirmed=true`, which the agent sets only after the human saw the quote. Sends an Idempotency-Key, so a retry never creates a second job or a second charge. |
| `job_status` | State and counts of a job (`GET /v1/jobs/{id}`), long poll up to 60 s. `done` does not mean every page worked: check `pages_failed` and `pages_not_run`. |
| `get_result` | Fetches `json`, `text`, `md` or `hocr`, writes it to a file and returns the path and size, never the content. Results expire after about 24 hours. |

`get_result` has no `pdf` format: a searchable PDF needs the original document and comes only from the `ocr-software` command line (`ocr-software scan.pdf --pipeline NAME@VERSION --format pdf -o out.pdf`).

`job_budget` is the most the job may cost, as a whole number of millicents (1 EUR = 100000); the job stops before a page that would exceed it. If the operator set no prices, it has no effect, but the tool still requires a whole number of at least 1.

Retries follow the agent guide: 429, 500, 503 and network errors up to 5 times with growing waits. A `Retry-After` over 60 seconds goes back to the human at once. Other 4xx errors are never retried.

## Settings

Environment variables only.

| Variable | Meaning |
|---|---|
| `OCR_KEY` | API key (starts with `ocr_`). Required except for `list_pipelines`. Never print it, never paste it into the chat. Get one at [ocr-software.com/order](https://ocr-software.com/order/). |
| `OCR_BASE_URL` | Default `https://api.ocr-software.com`. For a self-hosted service, for example `http://127.0.0.1:8080`. |
| `OCR_OUT_DIR` | Where `get_result` writes. Default `./ocr-results`. An existing file is never overwritten (`j1-1.json` instead). |
| `OCR_ALLOWED_DIR` | Optional. `quote` and `submit_job` then read only files below this folder; symlinks and `..` do not escape. |

## Safety rules built in

- The agent cannot start a paid job without `human_confirmed`, and the budget caps the cost: the job stops before a page that would exceed `job_budget`.
- Every document is checked locally before it is sent: PDF, PNG, JPEG or TIFF by its first bytes, a regular file, at most 100 MiB.
- Results go to files, not into the conversation, so large documents do not fill the context.
- The key is read from the environment and never appears in a tool result.

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

The names `OCR_SOFTWARE_API_KEY`, `OCR_SOFTWARE_BASE_URL` and `OCR_SOFTWARE_ALLOWED_DIR` of the `ocr-software` package and its CLI are read too; the `OCR_` names above win if both are set.

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
