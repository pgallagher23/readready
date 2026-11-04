# ---- Stage 1: build the iText tagger JAR ------------------------------------
FROM maven:3.9-eclipse-temurin-17 AS jar
WORKDIR /build
COPY output_gen/itext_src/pom.xml .
RUN mvn -q -B dependency:go-offline
COPY output_gen/itext_src/src ./src
RUN mvn -q -B clean package -DskipTests

# ---- Stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim-bookworm
ARG PREFETCH_MODELS=1

# System dependencies:
# - pango/cairo/gdk-pixbuf: WeasyPrint PDF generation
# - openjdk-17-jre-headless: iText PDF tagger (Java JAR)
# - libffi-dev: cffi compilation
# - poppler-utils: pdftotext for screen reader gate text comparison
# - tesseract-ocr: per-page OCR recovery for scanned pages (Phase 1c)
# - fonts-dejavu-core: default embedded font for iText tagger structure text
# - fonts-texgyre: CenturySchoolbook metric clone for iText width sync
# - fonts-liberation: Arial/Times/Courier metric clones — iText fallback + report CSS
# - fonts-urw-base35: PostScript base 35 Type1 .pfb + AFM for iText embedding
# - fonts-noto-core: broad Unicode fallback for iText embedded font
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    libcairo2 \
    libgl1 \
    libglib2.0-0 \
    openjdk-17-jre-headless \
    poppler-utils \
    tesseract-ocr \
    fonts-dejavu-core \
    fonts-texgyre \
    fonts-liberation \
    fonts-urw-base35 \
    fonts-noto-core \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /usr/local/share/fonts/texgyre \
    && ln -sf /usr/share/texmf/fonts/opentype/public/tex-gyre/*.otf /usr/local/share/fonts/texgyre/ \
    && fc-cache -f

WORKDIR /app
ENV PYTHONPATH=/app

# Install CPU-only PyTorch (saves ~4GB vs CUDA build).
# Step 1: install from the CPU-only wheel index.
# Step 2: pin the installed versions in a constraints file so that
#          requirements.txt (which pulls docling → torch) cannot
#          re-resolve torch from PyPI's CUDA build.
COPY requirements.txt .
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip freeze | grep -iE '^(torch|torchvision)==' > /tmp/torch-constraints.txt
RUN pip install --no-cache-dir -c /tmp/torch-constraints.txt -r requirements.txt \
    && rm -rf /tmp/torch-constraints.txt /root/.cache/pip

# Copy application code
COPY . .
COPY --from=jar /build/target/itext-tagger-1.0.0.jar /app/output_gen/itext_tagger.jar
RUN chmod +x /app/docker/entrypoint.sh

# Unprivileged runtime user, created before the model prefetch so the cache can
# be chowned in the same layer (a separate `chown -R` layer would copy every
# model file again). The font and fontconfig cache dirs are owned by it so the
# entrypoint can register mounted fonts even without the compose tmpfs mounts.
RUN useradd --system --uid 10001 --home /app readready \
    && mkdir -p /usr/local/share/fonts /var/cache/fontconfig \
    && chown readready /usr/local/share/fonts /var/cache/fontconfig

# Model cache location. docker-compose mounts the writable `hf-cache` volume
# here, so the cache stays usable under `read_only: true`.
ENV HF_HOME=/app/.hf

# PREFETCH_MODELS=1 (default) downloads the Docling layout and TableFormer
# models into the image at build time; scripts/prefetch_models.py initialises
# each layout pipeline the converter can select, which is what fetches the
# weights. The image then runs with the Hugging Face hub offline.
# PREFETCH_MODELS=0 skips the download: models are fetched on first use into
# HF_HOME, and the hub stays online.
RUN case "$PREFETCH_MODELS" in 0|1) ;; *) echo "PREFETCH_MODELS must be 0 or 1" >&2; exit 1;; esac \
    && if [ "$PREFETCH_MODELS" = "1" ]; then python scripts/prefetch_models.py \
    && rm -rf /root/.cache/pip /root/.cache/huggingface/hub/.locks /tmp/*; fi \
    && mkdir -p /app/.hf && chown -R readready /app/.hf

# HuggingFace isolation — disable telemetry and implicit token usage, and go
# offline when the models were prefetched (1) or stay online to download them
# (0). conversion.offline_models: true in config.yaml can only turn offline
# mode on; to let a prefetched container download a model, set
# HF_HUB_OFFLINE=0 and TRANSFORMERS_OFFLINE=0 in its environment.
# Loopback (app.py's default) is unreachable inside a container; compose/.env may override.
ENV READREADY_HOST=0.0.0.0
ENV HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    HF_HUB_OFFLINE=${PREFETCH_MODELS} \
    TRANSFORMERS_OFFLINE=${PREFETCH_MODELS}

# Generate AppCDS archive for faster JVM startup.
# Step 1: dump loaded class list by running the JAR in analyze-only mode
# (will fail with no input, but class list is still written).
# Step 2: create the shared archive from that class list.
# The .jsa file is picked up automatically by content_stream_analyzer.py
# and pdf_tagger.py when present.
RUN java -XX:DumpLoadedClassList=/app/output_gen/itext_classes.lst \
        -jar /app/output_gen/itext_tagger.jar --analyze 2>/dev/null ; \
    java -Xshare:dump \
        -XX:SharedClassListFile=/app/output_gen/itext_classes.lst \
        -XX:SharedArchiveFile=/app/output_gen/itext_tagger.jsa \
        -cp /app/output_gen/itext_tagger.jar 2>/dev/null ; \
    rm -f /app/output_gen/itext_classes.lst

# Run as the unprivileged user. Build-time work above ran as root; runtime writes
# go to /tmp, /app/output, /app/output_gen, the HF cache and fontconfig.
RUN mkdir -p /app/output \
    && chown -R readready /app/output /app/output_gen
USER readready

# Gradio default port
EXPOSE 7860

# The entrypoint registers user-supplied fonts (mounted at /app/fonts), then runs CMD.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Default: run the Gradio web UI
CMD ["python", "app.py"]
