FROM python:3.12-slim
WORKDIR /code
RUN apt-get update && apt-get install -y --no-install-recommends curl jq make && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "setuptools>=70.0.0,<72.0.0"
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

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
