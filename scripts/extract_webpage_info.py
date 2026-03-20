#!/usr/bin/env python3
"""Extract product info from unstructured webpages using an LLM.

Supports OpenAI, Anthropic (Claude), and Gemini-compatible HTTP APIs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SYSTEM_PROMPT = (
    "You extract product facts from messy webpage data. "
    "Return strict JSON only. If unknown, use null."
)

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": ["string", "null"]},
        "dimensions": {"type": ["string", "null"]},
        "included_components": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["price", "dimensions", "included_components", "confidence", "evidence"],
    "additionalProperties": False,
}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self.parts.append(cleaned)


@dataclass
class PageInput:
    source_url: str | None
    html: str
    text: str
    screenshots: list[Path]


def _http_request(url: str, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 90) -> dict[str, Any]:
    payload = None
    merged_headers = {"content-type": "application/json"}
    if headers:
        merged_headers.update(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    req = Request(url=url, method=method, data=payload, headers=merged_headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {text[:800]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc}") from exc


def fetch_html(url: str, timeout_seconds: int = 25) -> str:
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    req = Request(url=url, method="GET", headers=headers)
    with urlopen(req, timeout=timeout_seconds) as resp:
        return resp.read().decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    parser = TextExtractor()
    parser.feed(html)
    text = "\n".join(parser.parts)
    return re.sub(r"\n{2,}", "\n", text)


def load_page_input(url: str | None, html_file: Path | None, screenshots: list[Path]) -> PageInput:
    if url:
        html = fetch_html(url)
        source_url = url
    elif html_file:
        html = html_file.read_text(encoding="utf-8")
        source_url = None
    else:
        raise ValueError("Pass either --url or --html-file")
    return PageInput(source_url=source_url, html=html, text=html_to_text(html), screenshots=screenshots)


def _trim(s: str, max_chars: int) -> str:
    return s if len(s) <= max_chars else s[: max_chars - 3] + "..."


def build_extraction_prompt(page: PageInput) -> str:
    prompt = [
        "Extract these fields:",
        "- price",
        "- dimensions",
        "- included_components",
        "Rules:",
        "- Return JSON matching the schema.",
        "- Use null if field is missing.",
        "- included_components should be [] if nothing found.",
        "- Prefer product-detail sections over recommendations/ads.",
    ]
    if page.source_url:
        prompt.append(f"Source URL: {page.source_url}")
    body = "\n".join(prompt)
    body += "\n\n[VISIBLE_TEXT]\n" + _trim(page.text, 40_000)
    body += "\n\n[RAW_HTML]\n" + _trim(page.html, 40_000)
    return body


def call_openai(api_key: str, model: str, prompt: str, screenshots: list[Path]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for shot in screenshots:
        b64 = base64.b64encode(shot.read_bytes()).decode("utf-8")
        ext = shot.suffix.lstrip(".").lower() or "png"
        content.append({"type": "input_image", "image_url": f"data:image/{ext};base64,{b64}"})

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": content},
        ],
        "text": {"format": {"type": "json_schema", "name": "product_extraction", "schema": JSON_SCHEMA, "strict": True}},
    }
    data = _http_request(
        "https://api.openai.com/v1/responses",
        method="POST",
        headers={"authorization": f"Bearer {api_key}"},
        body=body,
    )
    return json.loads(data["output"][0]["content"][0]["text"])


def call_anthropic(api_key: str, model: str, prompt: str, screenshots: list[Path]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for shot in screenshots:
        b64 = base64.b64encode(shot.read_bytes()).decode("utf-8")
        mime = "image/png" if shot.suffix.lower() == ".png" else "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})

    body = {
        "model": model,
        "max_tokens": 1200,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": content + [{"type": "text", "text": "Return ONLY JSON matching this schema:\n" + json.dumps(JSON_SCHEMA)}],
            }
        ],
    }
    data = _http_request(
        "https://api.anthropic.com/v1/messages",
        method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        body=body,
    )
    return json.loads(data["content"][0]["text"])


def call_gemini(api_key: str, model: str, prompt: str, screenshots: list[Path]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for shot in screenshots:
        b64 = base64.b64encode(shot.read_bytes()).decode("utf-8")
        mime = "image/png" if shot.suffix.lower() == ".png" else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts + [{"text": "Return ONLY JSON matching this schema:\n" + json.dumps(JSON_SCHEMA)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    data = _http_request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        method="POST",
        body=body,
    )
    return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])


def run_extraction(provider: str, api_key: str, model: str, prompt: str, screenshots: list[Path]) -> dict[str, Any]:
    if provider == "openai":
        return call_openai(api_key=api_key, model=model, prompt=prompt, screenshots=screenshots)
    if provider == "anthropic":
        return call_anthropic(api_key=api_key, model=model, prompt=prompt, screenshots=screenshots)
    if provider == "gemini":
        return call_gemini(api_key=api_key, model=model, prompt=prompt, screenshots=screenshots)
    raise ValueError("--provider must be one of: openai, anthropic, gemini")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Webpage URL to fetch.")
    parser.add_argument("--html-file", type=Path, help="Path to saved HTML file.")
    parser.add_argument("--screenshot", type=Path, action="append", default=[], help="Optional screenshot path (repeatable).")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic", "gemini"])
    parser.add_argument("--model", required=True, help="Model name for the chosen provider.")
    parser.add_argument("--api-key", help="API key (or use provider env var).")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser.parse_args()


def resolve_api_key(provider: str, explicit_key: str | None) -> str:
    if explicit_key:
        return explicit_key
    env_map = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GOOGLE_API_KEY"}
    key = os.getenv(env_map[provider])
    if not key:
        raise ValueError(f"Missing API key. Pass --api-key or set {env_map[provider]}.")
    return key


def main() -> int:
    args = parse_args()
    page = load_page_input(url=args.url, html_file=args.html_file, screenshots=args.screenshot)
    prompt = build_extraction_prompt(page)
    api_key = resolve_api_key(provider=args.provider, explicit_key=args.api_key)
    result = run_extraction(args.provider, api_key, args.model, prompt, args.screenshot)
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
