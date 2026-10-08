import asyncio
import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlparse

import httpx

from . import prompts


class ModelUnavailable(RuntimeError):
    pass


async def validate_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("API URL must be public HTTPS on port 443, without credentials/query/fragment")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private/local API hosts are not allowed")
    return url.rstrip("/")


class LLM:
    def __init__(self, config, db):
        self.config, self.db = config, db
        self.client = httpx.AsyncClient(timeout=90, follow_redirects=False)
        self.slots = asyncio.Semaphore(config.concurrency)

    def settings(self):
        return self.db.get(
            "llm",
            {
                "base_url": self.config.base_url,
                "model": self.config.model,
                "key_env": self.config.key_env,
                "format": self.config.llm_format,
            },
        )

    async def request(self, system, data, schema=None, output=4000, image=None):
        cfg = self.settings()
        key = os.getenv(cfg["key_env"])
        if not key:
            raise ModelUnavailable(f"API key environment variable {cfg['key_env']} is not set")
        body = {
            "model": cfg["model"],
            "temperature": 0.1,
            "max_tokens": output,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
        }
        if image:
            body["model"] = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
            body["messages"][1]["content"] = [
                {"type": "text", "text": json.dumps(data, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        token_key = "max_tokens"
        if "api.groq.com" == urlparse(cfg["base_url"]).hostname:
            token_key = "max_completion_tokens"
            body[token_key] = body.pop("max_tokens")
            if "gpt-oss" in body["model"]:
                body["reasoning_effort"] = "low"
                body["include_reasoning"] = False
        json_schema = schema.model_json_schema() if schema else None
        if json_schema:
            # Strict providers require every property, including Pydantic fields with defaults.
            json_schema["required"] = list(json_schema["properties"])
            for prop in json_schema["properties"].values():
                prop.pop("default", None)
        if schema and cfg["format"] == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": json_schema,
                },
            }
        elif schema and cfg["format"] == "json":
            body["response_format"] = {"type": "json_object"}
        if schema:
            body["messages"][0]["content"] += "\nJSON schema: " + json.dumps(json_schema)
        async with self.slots:
            for attempt in range(3):
                try:
                    response = await self.client.post(
                        cfg["base_url"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + key},
                        json=body,
                    )
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(min(15, float(response.headers.get("retry-after", 2**attempt))))
                        continue
                    if response.is_error:
                        # Provider errors can contain key material or transcript excerpts: do not echo them.
                        raise ModelUnavailable(
                            f"Model API returned HTTP {response.status_code}; check model/format/quota"
                        )
                    result = response.json()
                    choice = result["choices"][0]
                    if choice.get("finish_reason") == "length":
                        if attempt < 2:
                            body[token_key] = min(body[token_key] * 2, 16000)
                            continue
                        raise ModelUnavailable(
                            "The model exhausted its output budget after retries. Try a smaller summary window."
                        )
                    content = choice["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("empty output")
                    if schema:
                        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
                        return schema.model_validate_json(content)
                    return content
                except (httpx.TransportError, ValueError, KeyError, IndexError, TypeError) as exc:
                    if attempt == 2:
                        raise ModelUnavailable(
                            "Model request failed or returned invalid output; no action taken"
                        ) from exc
                    await asyncio.sleep(2**attempt)
        raise ModelUnavailable("Model unavailable")

    async def models(self):
        cfg = self.settings()
        key = os.getenv(cfg["key_env"])
        if not key:
            raise ModelUnavailable("Configured API key environment variable is missing")
        try:
            response = await self.client.get(
                cfg["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}
            )
            if response.is_error:
                raise ModelUnavailable(f"Models API returned HTTP {response.status_code}")
            return sorted(m["id"] for m in response.json()["data"])
        except (httpx.TransportError, KeyError, ValueError) as exc:
            raise ModelUnavailable("Could not list models") from exc

    async def summarize(self, messages, requested, instruction):
        from collections import Counter
        from datetime import datetime, timezone

        notes, chunk, size = [], [], 0
        truncated = 0
        for msg in messages:
            text = msg["text"]
            if len(text) > 3000:
                text = text[:3000] + " [TEXT TRUNCATED]"
                truncated += 1
            item = {
                "id": msg["message_id"],
                "author": msg["name"],
                "user_id": msg["user_id"],
                "text": text,
                "media": msg["media"],
                "date": msg["date"],
                "deleted": bool(msg.get("deleted", False)),
            }
            encoded_size = len(json.dumps(item, ensure_ascii=False).encode())
            if chunk and (size + encoded_size > 12000 or len(chunk) >= 30):
                notes.append(
                    await self.request(prompts.CHUNK, {"instruction": instruction, "messages": chunk})
                )
                chunk, size = [], 0
            chunk.append(item)
            size += encoded_size
        if chunk:
            notes.append(await self.request(prompts.CHUNK, {"instruction": instruction, "messages": chunk}))
        # Hierarchical reduction keeps synthesis within smaller providers' context windows.
        while sum(len(n.encode()) for n in notes) > 28000 and len(notes) > 1:
            reduced = []
            for start in range(0, len(notes), 4):
                reduced.append(
                    await self.request(
                        prompts.CHUNK,
                        {"instruction": instruction, "evidence_notes": notes[start : start + 4]},
                    )
                )
            if len(reduced) == len(notes):
                break
            notes = reduced
        counts = Counter(str(m["user_id"]) + " " + (m["name"] or "anonymous") for m in messages)
        stats = {
            "requested": requested,
            "actual": len(messages),
            "text_truncated": truncated,
            "contributions": dict(counts),
            "media": dict(Counter(m["media"] for m in messages)),
            "from": datetime.fromtimestamp(messages[0]["date"], timezone.utc).isoformat(),
            "to": datetime.fromtimestamp(messages[-1]["date"], timezone.utc).isoformat(),
        }
        return await self.request(
            prompts.FINAL_SUMMARY,
            {"instruction": instruction, "statistics": stats, "notes": notes},
            output=8000,
        )

    async def close(self):
        await self.client.aclose()
