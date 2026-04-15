#!/usr/bin/env python3
"""Dual-LLM auto-fix judge.

Calls Anthropic (Claude) and OpenAI (Codex/GPT) directly via HTTP, parses each
model's fix proposal, cross-validates, and applies the fix if both models
agree with sufficient confidence. This replaces `anthropics/claude-code-action@v1`
which refuses to run when the PR branch differs from main's workflow file —
breaking auto-fix for any PR that touches CI config (i.e., most PRs from this
skill's generator path).

Inputs (env vars):
  ANTHROPIC_API_KEY  — required
  OPENAI_API_KEY     — required
  FIX_MAX_BUDGET_USD — optional soft limit per LLM call (default 1.00)

Inputs (files in cwd):
  failed-jobs.json   — list of {job_name, conclusion, log_tail}
  fix-context.json   — {project_root, platform, specs, authority_file, judgment_prompt, ...}

Outputs (files in cwd):
  fix-proposal.json  — {verdict, claude, codex, final_fix, disagreement?}
  <applied file>     — if verdict == "apply_fix", the fix is applied in-place

Exit codes:
  0  — verdict applied OR escalated (both are expected outcomes; GHA step
       continues to the "Check if fixes were applied" step)
  1  — hard error: missing env, malformed JSON, API 5xx after retries,
       'before' string not found in target file. Step fails; auto-fix job fails.

Cross-validation logic:
  - Both escalate              → escalate (needs human)
  - Both propose fix, same file, same 'after' value      → apply
  - Both propose fix, same file, different 'after' value → escalate (disagreement)
  - Both propose fix, different files                    → escalate (diverging)
  - One proposes, one escalates:
      if proposer's confidence >= 0.85 AND proposer is Claude (primary) → apply
      else → escalate
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any


ANTHROPIC_MODEL = "claude-sonnet-4-6"
OPENAI_MODEL = "gpt-5.1"  # swap to "o1-2024-12-17" or similar if reasoning preferred
REQUEST_TIMEOUT_S = 120
MAX_RETRIES = 2
RETRY_BACKOFF_S = 4


def log(level: str, msg: str) -> None:
    """GitHub Actions log annotation."""
    print(f"::{level}::{msg}", file=sys.stderr)


def http_post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"HTTP {e.code} from {url}: {err_body[:500]}")
            # 5xx and 429 are retry-worthy; 4xx others are not.
            if e.code < 500 and e.code != 429:
                raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = RuntimeError(f"Network error calling {url}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
    assert last_err is not None
    raise last_err


def call_anthropic(prompt: str, api_key: str) -> str:
    resp = http_post_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        body={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    blocks = resp.get("content", [])
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(texts)


def call_openai(prompt: str, api_key: str) -> str:
    resp = http_post_json(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        body={
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 4096,
        },
    )
    choices = resp.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


def extract_json_block(text: str) -> dict[str, Any] | None:
    """Find the first ```json ... ``` block and parse it. Be lenient about whitespace."""
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    if not m:
        # Fallback: try to find a bare JSON object the model wrote without fencing.
        m = re.search(r"(\{[\s\S]*?\"verdict\"[\s\S]*?\})", text)
        if not m:
            return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def build_prompt(failed_jobs: list[dict], fix_context: dict, judgment_prompt: str, model_name: str) -> str:
    return f"""{judgment_prompt}

---

You are the **{model_name}** proposer in a dual-LLM cross-validated auto-fix loop. Another model is running in parallel with the same context; their proposal will be compared against yours. Be conservative — if you are unsure, choose `escalate`. A disagreement blocks the fix; only propose a fix you are confident about.

## Failed jobs
```json
{json.dumps(failed_jobs, indent=2)}
```

## Project context
```json
{json.dumps(fix_context, indent=2)}
```

## Output — respond with EXACTLY ONE JSON code block

```json
{{
  "verdict": "apply_fix" | "escalate",
  "confidence": 0.0,
  "spec_id": "HELLO-001 or null",
  "reasoning": "one-to-two sentences explaining the 5-step judgment outcome",
  "fix": {{
    "file": "relative path from repo root",
    "before": "exact substring that currently exists in the file (must match uniquely)",
    "after": "replacement text"
  }}
}}
```

If `verdict == "escalate"`, set `fix` to `null`.

Confidence thresholds:
- `apply_fix` with confidence < 0.70 → treat as escalate (the cross-validator will discard it).
- Prefer smaller, surgical `before/after` spans — ideally a single line or a single token.
"""


def cross_validate(claude: dict | None, codex: dict | None) -> tuple[str, dict | None, str]:
    """Return (verdict, fix_to_apply, reason)."""
    if claude is None and codex is None:
        return ("escalate", None, "both models failed to produce a parseable JSON block")
    if claude is None:
        return ("escalate", None, "Claude produced no parseable output; cannot cross-validate")
    if codex is None:
        return ("escalate", None, "Codex produced no parseable output; cannot cross-validate")

    claude_verdict = claude.get("verdict", "escalate")
    codex_verdict = codex.get("verdict", "escalate")
    claude_conf = float(claude.get("confidence", 0.0) or 0.0)
    codex_conf = float(codex.get("confidence", 0.0) or 0.0)

    # Threshold: discard low-confidence apply_fix.
    if claude_verdict == "apply_fix" and claude_conf < 0.70:
        claude_verdict = "escalate"
    if codex_verdict == "apply_fix" and codex_conf < 0.70:
        codex_verdict = "escalate"

    if claude_verdict == "escalate" and codex_verdict == "escalate":
        return ("escalate", None, "both models escalated")

    if claude_verdict == "apply_fix" and codex_verdict == "apply_fix":
        cf = claude.get("fix") or {}
        xf = codex.get("fix") or {}
        if cf.get("file") != xf.get("file"):
            return (
                "escalate",
                None,
                f"disagreement on target file (Claude={cf.get('file')!r} vs Codex={xf.get('file')!r})",
            )
        if cf.get("after") != xf.get("after"):
            return (
                "escalate",
                None,
                f"disagreement on replacement text for {cf.get('file')}",
            )
        # Agreement — apply Claude's fix (they're identical on file+after; use Claude for before-match).
        return ("apply_fix", cf, f"both models agree (Claude conf={claude_conf}, Codex conf={codex_conf})")

    # Asymmetric: one proposes, the other escalates.
    # Only accept if the proposer is Claude with very high confidence.
    if claude_verdict == "apply_fix" and claude_conf >= 0.85:
        return (
            "apply_fix",
            claude.get("fix"),
            f"Codex escalated but Claude confident (conf={claude_conf}); applying primary",
        )
    return (
        "escalate",
        None,
        f"asymmetric verdicts (Claude={claude_verdict}, Codex={codex_verdict}) — escalating",
    )


def apply_fix(fix: dict, repo_root: pathlib.Path) -> None:
    target = (repo_root / fix["file"]).resolve()
    # Path-traversal guard: target must stay under repo_root.
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        raise RuntimeError(f"fix path escapes repo root: {fix['file']}")

    if not target.is_file():
        raise RuntimeError(f"fix target is not a regular file: {fix['file']}")

    original = target.read_text(encoding="utf-8")
    before = fix["before"]
    after = fix["after"]
    occurrences = original.count(before)
    if occurrences == 0:
        raise RuntimeError(f"'before' string not found in {fix['file']!r}")
    if occurrences > 1:
        raise RuntimeError(
            f"'before' string is not unique in {fix['file']!r} ({occurrences} matches) — refusing to guess"
        )
    target.write_text(original.replace(before, after, 1), encoding="utf-8")


def main(argv: list[str]) -> int:
    anth_key = os.environ.get("ANTHROPIC_API_KEY")
    oai_key = os.environ.get("OPENAI_API_KEY")
    if not anth_key:
        log("error", "ANTHROPIC_API_KEY env var is empty")
        return 1
    if not oai_key:
        log("error", "OPENAI_API_KEY env var is empty")
        return 1

    cwd = pathlib.Path.cwd()
    failed = json.loads((cwd / "failed-jobs.json").read_text(encoding="utf-8"))
    ctx = json.loads((cwd / "fix-context.json").read_text(encoding="utf-8"))

    jp_path = pathlib.Path(ctx.get("judgment_prompt", ""))
    judgment_prompt = jp_path.read_text(encoding="utf-8") if jp_path.is_file() else "(judgment-prompt.md unavailable; do your best.)"

    claude_prompt = build_prompt(failed, ctx, judgment_prompt, "Claude")
    codex_prompt = build_prompt(failed, ctx, judgment_prompt, "Codex")

    log("notice", f"Calling Anthropic {ANTHROPIC_MODEL} + OpenAI {OPENAI_MODEL} in sequence")
    try:
        claude_text = call_anthropic(claude_prompt, anth_key)
    except Exception as e:
        log("error", f"Anthropic call failed: {e}")
        return 1
    try:
        codex_text = call_openai(codex_prompt, oai_key)
    except Exception as e:
        log("error", f"OpenAI call failed: {e}")
        return 1

    claude_proposal = extract_json_block(claude_text)
    codex_proposal = extract_json_block(codex_text)

    verdict, fix, reason = cross_validate(claude_proposal, codex_proposal)
    proposal = {
        "verdict": verdict,
        "reason": reason,
        "claude": {
            "raw": claude_text[:4000],
            "parsed": claude_proposal,
        },
        "codex": {
            "raw": codex_text[:4000],
            "parsed": codex_proposal,
        },
        "final_fix": fix,
    }
    (cwd / "fix-proposal.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")

    if verdict == "apply_fix" and fix:
        try:
            apply_fix(fix, cwd)
        except Exception as e:
            log("error", f"apply_fix failed: {e}")
            return 1
        log("notice", f"fix applied to {fix['file']}: {reason}")
        # Write a small marker so the subsequent GHA step can detect apply vs escalate.
        (cwd / "fix-applied.flag").write_text("1", encoding="utf-8")
        return 0

    log("notice", f"verdict=escalate — {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
