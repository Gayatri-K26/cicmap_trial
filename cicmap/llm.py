"""DeepSeek client (OpenAI-compatible) with JSON validation, retries and a database cache."""
import hashlib
import json
from typing import Any, Optional

from . import config


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, conn, enabled: bool = True):
        self.conn = conn
        self.enabled = enabled and bool(config.DEEPSEEK_API_KEY)
        self.calls = 0          # real API calls made
        self.cache_hits = 0
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
        return self._client

    def json(self, purpose: str, version: int, system: str, user: str, max_tokens: int = 4000) -> Any:
        """Ask for a JSON object. Cached by (model, purpose, version, prompt)."""
        if not self.enabled:
            raise LLMError("LLM disabled (no API key or --no-llm)")
        key = hashlib.sha256(
            json.dumps([config.DEEPSEEK_MODEL, purpose, version, system, user]).encode()).hexdigest()
        row = self.conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        if row:
            self.cache_hits += 1
            return json.loads(row["response"])

        last_err: Optional[Exception] = None
        for _ in range(3):
            try:
                resp = self._get_client().chat.completions.create(
                    model=config.DEEPSEEK_MODEL,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=max_tokens,
                )
                self.calls += 1
                data = json.loads(resp.choices[0].message.content)
                self.conn.execute(
                    "INSERT OR REPLACE INTO llm_cache(key, purpose, model, response) VALUES (?, ?, ?, ?)",
                    (key, purpose, config.DEEPSEEK_MODEL, json.dumps(data)))
                self.conn.commit()
                return data
            except (json.JSONDecodeError, TypeError) as e:  # malformed JSON: retry
                last_err = e
            except Exception as e:  # network / API error: retry
                last_err = e
        raise LLMError(f"{purpose}: no valid JSON after 3 tries ({last_err})")
