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


def _parse_json_any_from_content(content: str) -> Any:
    """
    Parse either a JSON object or JSON array from model output.

    The prompt for batched photos returns a JSON array; legacy prompt returns an object.
    """
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
        "Follow all instructions in the system prompt.\n\n"
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


def analyze_site_photos_batch(
    *,
    settings: Settings,
    system_prompt: str,
    iroof_pdf_bytes: bytes,
    master_pricing_text: str,
    photos: list[dict[str, Any]],
    notes_combined: str,
    prepared_by_username: str,
) -> list[dict[str, Any]]:
    """
    Analyze up to 5 site photos in a single OpenRouter call.

    `photos` items must contain:
      - image_id: str (e.g., "img_1")
      - image_bytes: bytes
      - image_mime: str
      - filename: str (optional; used only to help the model, not required)
    Returns the parsed JSON array (one wrapper object per image).
    """
    if not photos:
        return []
    if len(photos) > 5:
        raise ValueError("photos batch size must be <= 5")

    iroof_b64 = base64.standard_b64encode(iroof_pdf_bytes).decode("ascii")

    user_prefix = (
        "Copy each image_id exactly as provided. "
        "Follow all instructions in the system prompt.\n\n"
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
            "type": "text",
            "text": user_prefix + f"## Estimator notes\n{notes_combined}\n",
        },
    ]

    for p in photos:
        image_id = str(p.get("image_id") or "").strip()
        if not image_id:
            raise ValueError("Each photo must include a non-empty image_id")
        image_bytes = p.get("image_bytes") or b""
        image_mime = str(p.get("image_mime") or "image/jpeg")
        if not image_mime.startswith("image/"):
            image_mime = "image/jpeg"
        img_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        filename = str(p.get("filename") or "").strip()

        label = f"IMAGE {image_id}"
        if filename:
            label += f" (filename: {filename})"

        user_content.append({"type": "text", "text": label})
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image_mime};base64,{img_b64}"},
            }
        )

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
    parsed = _parse_json_any_from_content(content)

    if isinstance(parsed, dict) and isinstance(parsed.get("batch_results"), list):
        parsed = parsed["batch_results"]

    if not isinstance(parsed, list):
        raise ValueError(f"Expected JSON array for batch response, got: {type(parsed).__name__}")

    if len(parsed) != len(photos):
        raise ValueError(
            f"Batch response length mismatch: expected {len(photos)} items, got {len(parsed)}"
        )

    # Ensure each item is a dict for downstream processing.
    out: list[dict[str, Any]] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"Batch item {i} is not an object")
        out.append(item)
    return out
