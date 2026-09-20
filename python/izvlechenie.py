"""
izvlechenie.py — one invoice, one API call, printed result.

Purpose: check the extraction prompt against a real model before any of it
goes into n8n. Not part of the pipeline. A bench tool.

Usage, from 04-kod/python:

    python izvlechenie.py --pdf "..\\..\\03-partiya\\obrazcy\\obrazecmaketC.pdf"
    python izvlechenie.py --pdf <file> --model gpt-5.6-luna --detail low
    python izvlechenie.py --pdf <file> --dry-run

Requires the environment variable OPENAI_API_KEY.
Uses the standard library only: no requests, no openai package.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.openai.com/v1/responses"

# Prices in USD per million tokens, checked 30 August 2026.
# Re-check before quoting a cost anywhere: these move.
PRICES = {
    "gpt-5.6-sol":   {"in": 4.00, "out": 20.00},
    "gpt-5.6-terra": {"in": 2.00, "out": 12.00},
    "gpt-5.6-luna":  {"in": 0.20, "out": 1.20},
}

DEFAULT_PROMPT = Path("..") / "prompts" / "extract-v1.md"


def load_prompt(path: Path):
    """
    Pull the system prompt and the user message out of the prompt file.

    The file is the single source of truth for prompt text. Splitting it here
    rather than keeping a copy in the script is deliberate: the same thing
    computed in two places drifts apart, and we have already been bitten by
    that once with the storage path.

    Sections are separated by lines containing only '---'. The 'Notes for us'
    section is never sent to the model.
    """
    if not path.exists():
        sys.exit(f"Prompt file not found: {path.resolve()}")

    blocks = path.read_text(encoding="utf-8").split("\n---\n")

    system_text = None
    user_text = None

    for block in blocks:
        stripped = block.strip()
        if stripped.startswith("## System prompt"):
            system_text = stripped[len("## System prompt"):].strip()
        elif stripped.startswith("## User message"):
            user_text = stripped[len("## User message"):].strip()

    # Fail loudly. A silently empty prompt would still produce an answer,
    # and that answer would look plausible.
    if not system_text:
        sys.exit(f"No '## System prompt' section found in {path}")
    if not user_text:
        sys.exit(f"No '## User message' section found in {path}")

    return system_text, user_text


def build_body(model, detail, system_text, user_text, pdf_path: Path):
    pdf_bytes = pdf_path.read_bytes()
    encoded = base64.b64encode(pdf_bytes).decode("ascii")

    return {
        "model": model,
        "instructions": system_text,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": pdf_path.name,
                        "file_data": f"data:application/pdf;base64,{encoded}",
                        "detail": detail,
                    },
                    {
                        "type": "input_text",
                        "text": user_text,
                    },
                ],
            }
        ],
        "max_output_tokens": 8000,
    }, len(pdf_bytes), len(encoded)


def call_api(body, api_key):
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        # Print the server's own words. Guessing at the cause of an API error
        # from its status code alone wastes more time than reading the body.
        detail_text = error.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP {error.code} from the API:\n{detail_text}")
    except urllib.error.URLError as error:
        sys.exit(f"Could not reach the API: {error.reason}")

    return json.loads(raw), time.time() - started


def collect_text(response):
    """Gather every output_text block. Reasoning blocks are skipped."""
    parts = []
    for item in response.get("output", []):
        for chunk in item.get("content", []):
            if chunk.get("type") == "output_text":
                parts.append(chunk.get("text", ""))
    return "\n".join(parts).strip()


def main():
    parser = argparse.ArgumentParser(
        description="Single extraction call against one invoice PDF")
    parser.add_argument("--pdf", required=True, help="path to the PDF file")
    parser.add_argument("--model", default="gpt-5.6-terra",
                        help="model id; default gpt-5.6-terra")
    parser.add_argument("--detail", default="auto", choices=["auto", "low", "high"],
                        help="how page images are processed; default auto")
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT),
                        help="path to the prompt file")
    parser.add_argument("--out", help="where to write the raw response JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the plan, send nothing")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path.resolve()}")

    system_text, user_text = load_prompt(Path(args.prompt))
    body, pdf_size, encoded_size = build_body(
        args.model, args.detail, system_text, user_text, pdf_path)

    print()
    print(f"  file:            {pdf_path.name}  ({pdf_size:,} bytes)")
    print(f"  base64 payload:  {encoded_size:,} bytes")
    print(f"  model:           {args.model}")
    print(f"  detail:          {args.detail}")
    print(f"  prompt:          {args.prompt}")
    print(f"  system prompt:   {len(system_text):,} characters")
    print()

    if args.dry_run:
        print("  This is the plan. Nothing was sent.")
        return

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set in this window.")

    print("  Calling the API...")
    response, elapsed = call_api(body, api_key)

    usage = response.get("usage", {})
    tokens_in = usage.get("input_tokens", 0)
    tokens_out = usage.get("output_tokens", 0)

    price = PRICES.get(args.model)
    if price:
        cost = tokens_in / 1e6 * price["in"] + tokens_out / 1e6 * price["out"]
        cost_text = f"${cost:.4f}"
        batch_text = f"${cost * 260:.2f}"
    else:
        cost_text = "unknown model, no price on file"
        batch_text = "-"

    print()
    print(f"  duration:        {elapsed:.1f} s")
    print(f"  input tokens:    {tokens_in:,}")
    print(f"  output tokens:   {tokens_out:,}")
    print(f"  cost:            {cost_text}")
    print(f"  x260 documents:  {batch_text}")
    print(f"  status:          {response.get('status', 'unknown')}")
    print()

    text = collect_text(response)
    if not text:
        print("  The model returned no text. Full response follows.")
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return

    # The prompt forbids markdown fences. Check whether that held.
    if text.startswith("```"):
        print("  WARNING: the answer is wrapped in a markdown fence.")
        print("  The prompt forbids this. Note it, do not silently strip it.")
        print()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        print(f"  The answer is not valid JSON: {error}")
        print("  Raw answer follows.")
        print(text)
        return

    print("  Valid JSON. Parsed answer:")
    print()
    print(json.dumps(parsed, indent=2, ensure_ascii=False))

    out_path = Path(args.out) if args.out else Path(
        f"otvet-{pdf_path.stem}-{args.model}.json")
    out_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print()
    print(f"  Saved to {out_path.resolve()}")


if __name__ == "__main__":
    main()
