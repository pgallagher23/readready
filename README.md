# ReadReady

ReadReady converts untagged or poorly tagged PDFs into accessible, tagged PDFs (WCAG 2.1 AA) while keeping the original page layout.

![The ReadReady web UI after processing examples/sample.pdf: a download link for sample_accessible.pdf, a page preview, and a Needs Review status with 23 of 24 WCAG 2.1 AA criteria passing (score 96%, up 8%), one major issue (figure missing caption), screen reader verification passed, and one AI-generated alt tag.](assets/screenshot.png)

## What it does

- Rebuilds document structure (headings, lists, tables, reading order) with Docling and injects a real tag tree into the **original** PDF, so the pages look the same and screen readers get the structure.
- Recovers text from pages Docling skips and runs OCR on scanned pages (Tesseract).
- Audits the document against 30+ WCAG 2.1 AA checks before and after remediation.
- Generates alt text, table captions, and formula descriptions with a vision-language model (local Ollama, or an OpenAI-compatible or Anthropic API).
- Produces the tagged PDF with an iText 9 tagger, plus an HTML report of what was checked and changed.

It ships as a command-line tool (`main.py`), which also processes whole directories, and a Gradio web UI (`app.py`).

A PDF that needs a password to open is rejected. Owner-password restrictions (no copy, no print) are removed so the file can be tagged, and the output PDF carries no restrictions.

## Requirements

| Component | Version | Used for |
|---|---|---|
| Python | 3.10 or newer (CI runs 3.10 and 3.12) | The pipeline, CLI, and web UIs |
| JDK | 17 or newer | Building and running the iText tagger JAR |
| Maven | 3.6 or newer | `make jar` |
| System packages | see below | Reports, OCR, text comparison |
| Ollama or an API key | optional | Alt text, table captions, formula descriptions |

On Debian or Ubuntu, install the same system packages the Docker image uses:

```bash
sudo apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libcairo2 libffi-dev \
    openjdk-17-jdk maven poppler-utils tesseract-ocr \
    fonts-dejavu-core fonts-texgyre fonts-liberation fonts-urw-base35 fonts-noto-core
```

This works on Ubuntu 22.04 and newer and on Debian 12. Debian 13 has no JDK 17 package; use `openjdk-21-jdk` instead.

`poppler-utils` provides `pdftotext`, which the screen-reader coverage and reading-order checks use. `tesseract-ocr` is needed only for scanned pages.

## Install from source

```bash
git clone <repo-url> readready && cd readready
make venv     # creates .venv, installs CPU-only PyTorch, then requirements.txt
make jar      # builds output_gen/itext_tagger.jar with Maven
make test     # optional: run the test suite
```

`make venv` installs the CPU build of PyTorch first so `requirements.txt` does not pull a multi-gigabyte CUDA build. To use a CUDA GPU, install the matching PyTorch build yourself before `pip install -r requirements.txt`; `conversion.device: "auto"` then uses it.

Without the tagger JAR the pipeline falls back to a WeasyPrint re-render, which does **not** keep the original layout. Run `make jar` before processing real documents.

## Run

```bash
.venv/bin/python main.py examples/sample.pdf -o out/   # command line
.venv/bin/python app.py                                # web UI on http://localhost:7860
```

The web UI listens on localhost only by default. Set `READREADY_HOST=0.0.0.0` to expose it on the network. Set `READREADY_SECRET` to require a login for the main UI (user `READREADY_USER`, default `readready`).

The first conversion downloads the Docling layout models from Hugging Face, which can take several minutes. Once they are cached you can set `conversion.offline_models: true` in `config.yaml` to stop all update checks (for air-gapped use).

`main.py` accepts a PDF or a directory of PDFs:

| Option | Meaning |
|---|---|
| `<pdf-or-dir>` | A PDF, or a directory of PDFs |
| `-o`, `--output` | Output directory (default `./output`) |
| `--config FILE` | Alternate config file (default `./config.yaml`, or `READREADY_CONFIG`) |
| `-j`, `--jobs N` | Parallel workers for a directory (default from `concurrency.batch_workers`) |
| `--max-heap 4g` | JVM heap for the tagger (overrides `resources.java_heap`) |
| `--resume` | Skip PDFs whose output already exists |
| `-v` | Debug logging |

Each document produces `<name>_accessible.pdf`, an HTML report of the checks and changes (`<name>_WCAG_validation_report.html`), and audit JSON in `<output>/<name>/`. The web UI can also download an accessible HTML version of the document.

Without Redis the web UI processes requests synchronously, one at a time. Gradio's usage analytics are turned off.

## Choosing a vision-language model

Alt text, table captions, and formula descriptions come from the model selected by `llm.provider` in `config.yaml`. Without a model, or if it cannot be reached, the run still completes: images get an empty alt, the document is marked Needs Review, and the web UI prompts you to add descriptions in the Alt Text & Captions tab before publishing.

### Local Ollama (default)

Install [Ollama](https://ollama.com), then `ollama pull qwen2.5vl:7b`. No key is needed and nothing leaves your machine. `llm.num_ctx` (default 8192) caps the context window and the GPU memory it reserves; lower it if the model competes with Docling's GPU layout pass.

### Hosted API with a key

A hosted model needs no GPU, but the images, tables, and formulas in each document are sent to the provider. Check that this is acceptable for your documents first.

1. Create a key with the provider: [Anthropic](https://console.anthropic.com/settings/keys), [OpenRouter](https://openrouter.ai/keys), or [OpenAI](https://platform.openai.com/api-keys).
2. Copy `.env.example` to `.env` in the repository folder and fill in the matching variable, for example:

   ```bash
   cp .env.example .env
   # then edit .env:
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   `main.py` and `app.py` read `.env` from the folder you start them in; an exported environment variable works too. `.env` is ignored by git. Do not put keys in `config.yaml`.
3. Set the provider in the `llm:` section of `config.yaml`:

   | Provider | `llm.provider` | Key variable | Also set |
   |---|---|---|---|
   | Anthropic | `"anthropic"` | `ANTHROPIC_API_KEY` | `anthropic_model` (optional) |
   | OpenRouter | `"openai"` | `OPENROUTER_API_KEY` | `openai_model` (optional; any vision model OpenRouter lists) |
   | OpenAI, or another OpenAI-compatible server | `"openai"` | `OPENAI_API_KEY` | `openai_base_url` (for OpenAI, `https://api.openai.com/v1`) and an `openai_model` that accepts images |

   With `"openai"`, `OPENROUTER_API_KEY` wins if both keys are set.
4. Restart the web UI, then process `examples/sample.pdf`. Its figure should get a description in the Alt Text & Captions tab.

`llm.max_images_per_document` and `llm.max_tables_per_document` (default 200 each) cap the model calls, and so the cost, per document.

## Docker

```bash
cp .env.example .env          # set provider keys and a real REDIS_PASSWORD
docker compose up --build -d  # web UI, Celery workers, Redis, Caddy
```

Then open http://localhost. The app listens on port 7860 inside the Docker network only; Caddy publishes ports 80 and 443, which must be free. If another web server already uses them, change the host side of the `caddy` ports (for example `"8080:80"`, then open http://localhost:8080). For local testing without Caddy, uncomment the `ports` block under the `app` service.

### AI models with Docker

Without a model the stack still works, but every image is left for you to describe. To add one:

- **API key.** Put the key in the same `.env` file as `REDIS_PASSWORD` and set `llm.provider` in `./config.yaml` as described in [Hosted API with a key](#hosted-api-with-a-key). Apply the change with `docker compose up -d --force-recreate app worker`.
- **Ollama on the Docker host.** Inside a container, `localhost` is the container itself, so set `llm.base_url: "http://host.docker.internal:11434"` in `./config.yaml` (the compose file maps that name to the host), then recreate `app` and `worker` as above. On Linux, Ollama listens only on 127.0.0.1 by default, which containers cannot reach: run `sudo systemctl edit ollama`, add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`, and restart Ollama. Port 11434 is then open to your network too, so block it at your firewall if the machine is reachable from outside. Docker Desktop on macOS and Windows needs no Ollama change.

`./config.yaml` is shared with source runs from the same folder; to keep the source install on `localhost`, point `READREADY_CONFIG` at a separate copy when running from source.

The default stack serves the web UI through Caddy on plain HTTP port 80 with no login and no TLS. Before exposing it beyond your own machine, set `READREADY_SECRET` (and optionally `READREADY_USER`) in `.env` to turn on the login, switch to the HTTPS site block in the `Caddyfile`, and set a real `REDIS_PASSWORD`.

- The image pre-fetches every Docling layout model and TableFormer when built with `PREFETCH_MODELS=1` (the default) and then runs with the Hugging Face hub offline. Build with `--build-arg PREFETCH_MODELS=0` for a smaller image; models then download on first use.
- The model cache lives in the `hf-cache` volume (`/app/.hf`), which stays writable while the containers are read-only. With prefetched models it is filled from the image on first start; after rebuilding the image with new models, remove it (`docker compose down`, then `docker volume rm <project>_hf-cache`) so it is refilled.
- `./config.yaml` is mounted read-only at `/app/config.yaml`, and `./fonts` at `/app/fonts`.
- `make docker` builds the image alone (tag `readready:dev`).

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | VLM provider keys (only the one matching `llm.provider`) |
| `REDIS_PASSWORD`, `REDIS_URL` | Task queue connection (Docker and Celery) |
| `READREADY_SHARED_DIR` | Directory shared with Celery workers (default `/app/output`, the compose volume) |
| `READREADY_HOST` | Address the web UI binds to (default `127.0.0.1`; Docker uses `0.0.0.0`) |
| `READREADY_USER`, `READREADY_SECRET` | Main web UI login; enabled only when `READREADY_SECRET` is set (user defaults to `readready`) |
| `READREADY_CONFIG` | Path to a config file outside the repository |
| `READREADY_FONT_DIR` | Directory of user-supplied fonts (default `fonts/`) |

## Verdicts

Every document gets a verdict of **Pass** or **Needs Review**, using the thresholds in the `verdict:` section of `config.yaml`. A Pass is an automated result, not a certification: ReadReady confirms alt text exists, not that it is good. Spot-check anything high-stakes with a screen reader.

## Fonts

No proprietary fonts are distributed. Without metric-matched fonts the tagger falls back to free fonts with close metrics (Liberation, TeX Gyre, Noto, DejaVu); the output is still fully tagged, though some glyph shapes and line breaks may differ from the original.

To import fonts you are licensed to use (for example from a Windows or macOS machine you own):

```bash
make fonts SRC=/mnt/c/Windows/Fonts   # copies into fonts/, never overwrites
make fonts-check                      # shows which families are covered
```

## Configuration

All settings live in `config.yaml`, and each one is commented in place. Point `READREADY_CONFIG` at a copy outside the repository to keep local settings separate.

| Section | Controls |
|---|---|
| `llm` | Model provider, model names, context window, VLM limits |
| `conversion` | Docling options, OCR, text recovery, heading detection |
| `audit` | Audit report options: `severity_threshold` (lowest severity reported, default `minor`), `include_remediated`, `detailed` (element context in each issue). Every WCAG check always runs. |
| `verdict` | Thresholds for Pass versus Needs Review |
| `ocr_quality` | Scoring of OCR text on scanned pages |
| `formulas` | Formula captions and MathML |
| `table_analysis` | LLM fallback for ambiguous table headers |
| `remediation` | Which HTML fixes are applied |
| `output` | PDF engine, tag mode, reports |
| `ui` | Upload limits |
| `branding` | Name, logo, colors, start screen |
| `editor` | Content Editor input limits |
| `resources` | JVM heap and RAM budget |
| `concurrency` | Parallel workers |
| `queue` | Celery and Redis settings |

### Branding

Everything you customize lives in the `branding:` section: product name, header title, organization, logo, colors, and the start-screen text. Put the logo under `static/branding/`; see `static/branding/README.md`.

## Troubleshooting

| Problem | What to check |
|---|---|
| Output loses the original layout | The tagger JAR is missing; run `make jar` |
| First run is slow or hangs | The Docling models are downloading; it needs network access the first time |
| Alt text and captions are blank | The model server is not running (`ollama serve`, `ollama list`), the API key is missing from `.env`, or `llm.provider` does not match the key |
| Alt text is blank in Docker only | The containers cannot reach Ollama; see [AI models with Docker](#ai-models-with-docker) |
| `docker compose up` fails with "port is already allocated" or "address already in use" | Another service uses port 80 or 443; change the `caddy` ports |
| "JVM heap exhausted" | Pass `--max-heap 4g` or set `resources.java_heap` |
| Machine swapping on a directory run | Each worker loads the Docling models (2 to 3 GB); lower `-j` or `concurrency.batch_workers` |
| Upload rejected in the web UI | Too many files or pages (`ui.max_pages`), too large, encrypted, or not a PDF; limits are in `ui:` |
| Text copies as garbage, very low coverage | The source font has no Unicode mapping; process the original digital source instead |

## License

Licensed under AGPL-3.0-or-later; see [LICENSE](LICENSE). The tagger is built on iText and the PDF libraries use PyMuPDF, both AGPL-3.0, so the combined work is AGPL. Third-party components are listed in [NOTICE](NOTICE).

## Acknowledgements

The WCAG audit checks and several remediation strategies began as modified copies of Amazon's [Content Accessibility Utility on AWS](https://github.com/awslabs/content-accessibility-utility-on-aws) (Apache-2.0), which we found through the ASU AI Cloud Innovation Center's [PDF_Accessibility](https://github.com/ASUCICREPO/PDF_Accessibility) project. The derived files carry the original copyright notice; the license text is in [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt).

## Status

Version 1.0.0. See [CHANGELOG.md](CHANGELOG.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
