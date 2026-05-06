"""Probe HF Inference Providers for the first model that responds.

Usage:
    python -m scripts.probe_hf_models

Tries a hand-picked priority list of small/common open-weight chat
models and reports which (if any) the current HF_TOKEN can reach.
The intent is to identify a working slug to use for the comparative
red-team run before committing to a full 60-attack suite that might
403 on every call.

Exit code 0 = at least one model works; 1 = all candidates failed.
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv
from huggingface_hub import InferenceClient
from huggingface_hub.errors import HfHubHTTPError

# Priority order: smaller / more broadly served first. The first one
# that returns a successful completion wins.
CANDIDATES = [
    "meta-llama/Llama-3.2-3B-Instruct",
    "Qwen/Qwen2.5-1.5B-Instruct",
    "HuggingFaceH4/zephyr-7b-beta",
    "mistralai/Mistral-7B-Instruct-v0.3",
]


def _classify_error(exc: Exception) -> tuple[str, int | None]:
    """Return (label, status_code) for an exception we caught."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__, status


def probe_one(model: str, token: str) -> tuple[bool, str, int | None, float]:
    """Attempt a 1-token completion against `model`.

    Returns (ok, error_label, status_code, latency_seconds). On success,
    error_label is the empty string and status_code is None.
    """
    client = InferenceClient(model=model, token=token, timeout=30)
    t0 = time.monotonic()
    try:
        resp = client.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0,
            max_tokens=1,
        )
        latency = time.monotonic() - t0
        # Sanity-check the response shape; some endpoints will 200 but
        # return an empty/malformed body.
        _ = resp.choices[0].message.content
        return True, "", None, latency
    except HfHubHTTPError as e:
        latency = time.monotonic() - t0
        label, status = _classify_error(e)
        return False, label, status, latency
    except Exception as e:
        latency = time.monotonic() - t0
        label, status = _classify_error(e)
        return False, label, status, latency


def main() -> int:
    load_dotenv()
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN missing from environment / .env. Aborting.")
        print(
            "Get a token at https://huggingface.co/settings/tokens with the "
            "'Make calls to Inference Providers' scope enabled."
        )
        return 1

    print(f"Probing {len(CANDIDATES)} candidate models...")
    print()

    results: list[tuple[str, bool, str, int | None, float]] = []
    winner: str | None = None

    for model in CANDIDATES:
        print(f"  -> {model} ... ", end="", flush=True)
        ok, label, status, latency = probe_one(model, token)
        results.append((model, ok, label, status, latency))
        if ok:
            print(f"OK ({latency * 1000:.0f}ms)")
            winner = model
            break
        else:
            status_str = f" [{status}]" if status is not None else ""
            print(f"FAIL: {label}{status_str} ({latency * 1000:.0f}ms)")

    print()
    print("=" * 60)
    print("Per-candidate summary:")
    for model, ok, label, status, latency in results:
        if ok:
            print(f"  [OK]   {model}  latency={latency * 1000:.0f}ms")
        else:
            status_str = f" status={status}" if status is not None else ""
            print(f"  [FAIL] {model}  err={label}{status_str}")

    print()
    if winner is not None:
        print(f"RECOMMENDED MODEL: {winner}")
        return 0

    # All failed — diagnose.
    all_403 = all(
        (not ok) and status == 403 for _model, ok, _label, status, _lat in results
    )
    if all_403:
        print("All candidates returned 403 Forbidden.")
        print()
        print("Your HF_TOKEN is missing the 'Make calls to Inference Providers' scope.")
        print("Fix:")
        print("  1. Go to https://huggingface.co/settings/tokens")
        print("  2. Create a new fine-grained token (or edit the current one)")
        print("  3. Enable: 'Make calls to Inference Providers'")
        print("  4. Replace HF_TOKEN in .env with the new token")
        print("  5. Re-run this probe")
        return 1

    print("All candidates failed — but not uniformly with 403.")
    print("Inspect the per-candidate summary above and diagnose by error label.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
