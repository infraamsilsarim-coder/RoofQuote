import base64
import json
import re
from typing import Any

import httpx

from app.config import Settings


API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _parse_json_from_content(content: str) -> dict[str, Any]:
    text = content.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    for candidate in (text, content.strip()):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Model did not return valid JSON. Snippet: {content[:500]!r}")


def analyze_site_photo(
    *,
    settings: Settings,
    system_prompt: str,
    iroof_pdf_bytes: bytes,
    master_pricing_text: str,
    image_bytes: bytes,
    image_mime: str,
    notes_combined: str,
    prepared_by_username: str,
) -> dict[str, Any]:
    iroof_b64 = base64.standard_b64encode(iroof_pdf_bytes).decode("ascii")
    img_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    if not image_mime.startswith("image/"):
        image_mime = "image/jpeg"
    user_prefix = (
        f"In your JSON output, set report_info.prepared_by to exactly: {prepared_by_username!r}. "
        "Follow all other instructions in the system prompt.\n\n"
    )
    user_content: list[dict[str, Any]] = [
        {
            "type": "file",
            "file": {
                "filename": "iroof_report.pdf",
                "file_data": f"data:application/pdf;base64,{iroof_b64}",
            },
        },
        {
            "type": "text",
            "text": (
                "## Master pricing workbook\n"
                "Tab-separated rows per sheet. Use only these rates and SKUs.\n\n"
                f"{master_pricing_text}"
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{img_b64}"},
        },
        {
            "type": "text",
            "text": user_prefix + f"## Estimator notes\n{notes_combined}\n",
        },
    ]

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://rangerroofing.com",
        "X-Title": "RoofQuote AI",
    }
    payload = {
        "model": settings.openrouter_model,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    with httpx.Client(timeout=300.0) as client:
        r = client.post(API_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    if "error" in data:
        raise RuntimeError(str(data["error"]))

    content = data["choices"][0]["message"]["content"]
    return _parse_json_from_content(content)
