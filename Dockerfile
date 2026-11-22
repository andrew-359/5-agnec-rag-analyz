FROM node:24-bookworm-slim AS web
ENV CI=true
WORKDIR /app/web
RUN npm install --global pnpm@10.30.0
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/tsconfig.json web/tsup.config.ts web/vite.config.ts web/index.html ./
COPY web/src/ ./src/
COPY web/tests/ ./tests/
COPY web/scripts/ ./scripts/
RUN pnpm typecheck && pnpm build && pnpm prune --prod

FROM python:3.12-slim-bookworm
ENV PATH="/opt/rag/bin:${PATH}" \
    RAG_PYTHON=/opt/rag/bin/python \
    HF_HOME=/opt/huggingface \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update -o Acquire::Retries=2 \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip python -m venv /opt/rag \
    && pip install --index-url https://download.pytorch.org/whl/cpu \
        torch==2.8.0+cpu torchvision==0.23.0+cpu \
    && pip install -r /tmp/requirements.txt
COPY requirements-ocr-onnx.txt /tmp/requirements-ocr-onnx.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/requirements-ocr-onnx.txt 'pypdf>=6,<8' 'PyMuPDF>=1.24'

WORKDIR /app
COPY rag_mvp/ ./rag_mvp/
RUN --mount=type=cache,target=/root/.cache/rag-models,sharing=locked \
    HF_HOME=/root/.cache/rag-models HF_HUB_DISABLE_XET=1 \
    python -c 'from rag_mvp.strategies.parser import InputFormat, converter_for, docling_options; converter_for(docling_options(do_ocr=False)).initialize_pipeline(InputFormat.PDF)' \
    && cp -a /root/.cache/rag-models/. /opt/huggingface \
    && chmod -R a+rX /opt/huggingface
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /app/web/dist ./web/dist
COPY --from=web /app/web/node_modules ./web/node_modules
COPY --from=web /app/web/package.json ./web/package.json
COPY web/demo/ ./web/demo/
COPY docs/research/controls/comparison-scopes.json ./docs/research/controls/
RUN groupadd --gid 10001 rag && useradd --uid 10001 --gid rag --create-home rag \
    && install -d -o rag -g rag -m 0700 /app/output/web-articles /app/output/publications
USER rag
RUN python -c 'from rag_mvp.strategies.parser import InputFormat, converter_for, docling_options; converter_for(docling_options(do_ocr=False)).initialize_pipeline(InputFormat.PDF)'
WORKDIR /app/web
ARG SOURCE_COMMIT=local
LABEL org.opencontainers.image.revision=${SOURCE_COMMIT}
ENV RAG_HOST=0.0.0.0 PORT=4310 RAG_RELEASE_SHA=${SOURCE_COMMIT}
EXPOSE 4310
CMD ["node", "dist/server/index.js"]
