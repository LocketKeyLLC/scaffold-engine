"""§17.1061 — load profile through an LLM path (make load-llm).

Targets a THROWAWAY orchestrator whose roles are pinned to the stub
provider, so every request walks the real router, auth, trace capture and
OpenAI-compat wire code with a canned model behind it. What it measures is
the engine's own ceiling per worker, not model latency.
"""
import os

from locust import HttpUser, between, task


class ChatUser(HttpUser):
    wait_time = between(0.1, 0.4)

    def on_start(self):
        key = os.environ.get("SCAFFOLD_API_KEY", "")
        self.client.headers.update({"X-API-Key": key, "Authorization": f"Bearer {key}"} if key else {})

    @task(4)
    def chat_completion(self):
        self.client.post("/v1/chat/completions", name="/v1/chat/completions",
                         json={"model": "stub", "stream": False,
                               "messages": [{"role": "user", "content": "What is the state of the build?"}]})

    @task(1)
    def health(self):
        self.client.get("/health", name="/health")
