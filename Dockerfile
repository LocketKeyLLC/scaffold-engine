FROM python:3.12.13-slim
WORKDIR /code
RUN apt-get update && apt-get install -y --no-install-recommends curl jq make && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "setuptools==71.1.0"
# TODO(#85): Split dev deps into a separate build stage to shrink the
# production image. Currently requirements-dev is installed in the same
# layer; future work: multi-stage build with a dev target for tests and
# a lean runtime target that only has requirements.txt.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements-dev.txt

# Task #16: Pre-download reranker weights at build time so first /rag
# request doesn't block on a HuggingFace download.
RUN python -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('tomaarsen/Qwen3-Reranker-0.6B-seq-cls', \
                  cache_dir='/code/.cache/huggingface')"

COPY app/ app/
COPY tests/ /code/tests/
COPY Makefile /code/Makefile
COPY pyproject.toml /code/pyproject.toml
COPY scripts/ /code/scripts/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
