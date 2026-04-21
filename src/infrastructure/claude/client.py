"""
Unified Claude CLI client for claude-parallel.

Consolidates Claude subprocess invocation, error classification,
retry logic, and output sanitization from chat_cli.py and merger.py.
"""

import asyncio
import logging
import subprocess
import sys
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Output sanitization ──────────────────────────────────

def strip_code_fences(text: str) -> str:
    """Remove surrounding markdown code fences if present.

    Handles both ````lang ... ``` and bare ``` ... ``` patterns.
    """
    lines = text.split("\n")

    # Remove leading fence (may include language tag like ```yaml)
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]

    # Remove trailing fence(s)
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines)


def strip_code_fences_simple(text: str) -> str:
    """Simple code fence stripping for inline text (not line-based).

    Used for call_claude output that may be wrapped in ```yaml ... ```.
    """
    out = text
    if out.startswith("```yaml"):
        out = out[7:]
    elif out.startswith("```"):
        out = out[3:]
    if out.endswith("```"):
        out = out[:-3]
    return out.strip()


# ── Error classification ─────────────────────────────────

def is_budget_error(text: str) -> bool:
    t = (text or "").lower()
    return "exceeded usd budget" in t or ("budget" in t and "exceed" in t)


def is_quota_error(text: str) -> bool:
    t = (text or "").lower()
    return (
        "usage_limit_reached" in t
        or "insufficient_quota" in t
        or ("quota" in t and "exceed" in t)
    )


def is_retryable_error(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "rate limit", "429", "503", "overloaded", "timeout",
        "connection", "econnreset", "temporary", "try again",
    ]
    return any(p in t for p in patterns)


def is_turns_error(text: str) -> bool:
    t = (text or "").lower()
    return "max turns" in t or "max_turns" in t


def parse_model_chain(models_arg: str) -> List[str]:
    """Parse comma-separated model chain. Empty string → use default."""
    if not models_arg:
        return [""]
    models = [m.strip() for m in models_arg.split(",") if m.strip()]
    return models if models else [""]


# ── Synchronous Claude CLI invocation ────────────────────

def call_claude(
    prompt: str,
    system: str = "",
    budget: float = 0.8,
    retries: int = 2,
    model_chain: Optional[List[str]] = None,
    *,
    max_turns: int = 6,
    timeout: int = 180,
    output_format: str = "text",
    cwd: Optional[str] = None,
    budget_ceiling: float = 3.0,
    budget_escalation: float = 1.8,
) -> Tuple[bool, str, str]:
    """Invoke claude CLI with automatic retry and model fallback.

    Returns (success, output, error_message).

    Retry strategy:
    - Budget exceeded: escalate budget up to ceiling
    - Transient errors: exponential backoff
    - Model fallback: try next model in chain
    - Quota errors: fail fast (user action required)
    - Turns errors: break (same params will fail again)
    """
    full_prompt = prompt
    if system:
        full_prompt = f"{system}\n\n---\n\n{prompt}"

    max_attempts = max(1, retries + 1)
    chain = model_chain or [""]
    last_error = "未知错误"

    for model_idx, model_name in enumerate(chain):
        current_budget = max(0.1, float(budget))
        _log_model_start(model_name, model_idx, len(chain))

        for attempt in range(1, max_attempts + 1):
            cmd = [
                "claude", "-p", full_prompt,
                "--output-format", output_format,
                "--max-turns", str(max_turns),
                "--max-budget-usd", f"{current_budget:.3f}",
                "--dangerously-skip-permissions",
            ]
            if model_name:
                cmd.extend(["--model", model_name])

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                )

                stdout_text = (result.stdout or "").strip()
                stderr_text = (result.stderr or "").strip()
                output = stdout_text if stdout_text else stderr_text

                logger.debug(
                    "call_claude rc=%d stdout=%dc stderr=%dc",
                    result.returncode, len(stdout_text), len(stderr_text),
                )

                if result.returncode == 0 and output:
                    cleaned = strip_code_fences_simple(output)
                    if cleaned.lower().startswith("error:"):
                        logger.debug("Recognized as error: %s", cleaned[:200])
                        last_error = cleaned
                    else:
                        return (True, cleaned, "")
                else:
                    last_error = output or f"claude exit code={result.returncode}"
                    logger.debug("Failure: %s", last_error[:200])

                # Retry decision
                if attempt < max_attempts:
                    action = _retry_decision(last_error, current_budget, budget_ceiling, budget_escalation)
                    if action == "break":
                        break
                    elif action.startswith("escalate:"):
                        new_budget = float(action.split(":")[1])
                        current_budget = new_budget
                        time.sleep(1.0)
                        continue
                    elif action.startswith("backoff:"):
                        delay = float(action.split(":")[1])
                        time.sleep(delay)
                        continue

                break

            except subprocess.TimeoutExpired:
                last_error = f"调用超时 ({timeout}s)"
                if attempt < max_attempts:
                    backoff = min(3 * (2 ** (attempt - 1)), 20)
                    logger.warning("Claude call timed out, retry %d/%d after %ds", attempt + 1, max_attempts, backoff)
                    time.sleep(backoff)
                    continue
                break

            except FileNotFoundError:
                logger.error("claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")
                sys.exit(1)

        # Model fallback
        if model_idx < len(chain) - 1:
            logger.info("Current model failed, falling back to next model...")

    return (False, "", last_error)


def _log_model_start(model_name: str, idx: int, total: int) -> None:
    if model_name:
        logger.info("Using model: %s (%d/%d)", model_name, idx + 1, total)
    else:
        logger.info("Using default model (%d/%d)", idx + 1, total)


def _retry_decision(error: str, current_budget: float, ceiling: float, escalation: float) -> str:
    """Decide retry action based on error type.

    Returns: 'break', 'escalate:<new_budget>', or 'backoff:<delay>'
    """
    if is_quota_error(error):
        return "break"
    if is_turns_error(error):
        return "break"
    if is_budget_error(error):
        next_budget = min(current_budget * escalation, ceiling)
        if next_budget > current_budget + 0.01:
            return f"escalate:{next_budget}"
        return "break"
    if is_retryable_error(error):
        return "backoff:3"  # caller applies exponential scaling
    return "break"


# ── Async Claude CLI invocation ──────────────────────────

async def call_claude_async(
    prompt: str,
    *,
    timeout: int = 300,
    cwd: Optional[str] = None,
    output_format: str = "text",
) -> Tuple[bool, str, str]:
    """Invoke claude CLI asynchronously (for merger conflict resolution).

    Returns (success, output, error_message).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "--output-format", output_format,
            cwd=cwd or ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning("Async Claude call timed out (%ds)", timeout)
        return (False, "", f"timed out ({timeout}s)")
    except FileNotFoundError:
        logger.error("claude CLI not found for async call")
        return (False, "", "claude CLI not found")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip() if stderr else "unknown error"
        return (False, "", err)

    output = stdout.decode("utf-8", errors="replace")
    return (True, output, "")
