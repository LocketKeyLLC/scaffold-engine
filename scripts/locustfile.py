"""§17.1057 — smoke load profile for the orchestrator (make load-smoke).

Only cheap, model-free endpoints: /health (unauthenticated) and /status
(X-API-Key). The point is the single-worker uvicorn ceiling under concurrent
readers, not the LLM paths — those cost money per request.
"""
import os

from locust import HttpUser, between, task


class Reader(HttpUser):
    wait_time = between(0.2, 0.8)

    def on_start(self):
        key = os.environ.get("SCAFFOLD_API_KEY", "")
        self.client.headers.update({"X-API-Key": key} if key else {})

    @task(3)
    def health(self):
        self.client.get("/health", name="/health")

    @task(1)
    def status(self):
        self.client.get("/status", name="/status")
