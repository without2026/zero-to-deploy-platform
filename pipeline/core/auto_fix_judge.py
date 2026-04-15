#!/usr/bin/env python3
"""Auto-fix proposer (human-in-the-loop).

Calls Anthropic (Claude) once to analyze the CI failure and write a
proposal to `fix-proposal.json`. **Does not apply the fix.** A downstream
GHA step posts the proposal as a PR comment and labels the PR so the CTO
can cross-validate (e.g., via `/codex:review` locally) and decide whether
to apply the proposed change or correct it by hand.

Why not auto-apply?
  - GitHub Actions can't use the CTO's ChatGPT OAuth for Codex cross-
    validation without complicated token plumbing and a security downgrade.
  - A one-LLM auto-apply loop burned more time writing wrong fixes than it
    saved; human review of a single proposal is cheaper than reverting an
    auto-commit.
  - Claude is still doing 100% of the diagnostic work — the CTO's job is
    just the confirm/reject gate, not the 5-step judgment itself.

Why not multi-LLM here?
  - Adding Codex (OpenAI API) required a billing relationship the project
    doesn't have; ChatGPT subscription doesn't work for server-side calls.
  - Cross-validation happens at the human tier now. That's the Q3-consistent
    answer: the human is the second validator, not a second LLM.

Inputs (env vars):
  ANTHROPIC_API_KEY     — required

Inputs (files in cwd):
  failed-jobs.json      — list of {job_name, conclusion, log_tail}
  fix-context.json      — {project_root, platform, specs, authority_file,
                           judgment_prompt, ...}

Outputs (files in cwd):
  fix-proposal.json     — {verdict, confidence, spec_id, reasoning,
                           fix: {file, before, after}}, always written

Exit codes:
  0 — proposal written successfully (whatever the verdict)
  1 — hard error: missing env, malformed inputs, API 5xx after retries
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
REQUEST_TIMEOUT_S = 120
MAX_RETRIES = 2
RETRY_BACKOFF_S = 4


def log(level: str, msg: str) -> None:
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
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def extract_json_block(text: str) -> dict[str, Any] | None:
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    if not m:
        m = re.search(r"(\{[\s\S]*?\"verdict\"[\s\S]*?\})", text)
        if not m:
            return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def build_prompt(failed_jobs: list[dict], fix_context: dict, judgment_prompt: str) -> str:
    return f"""{judgment_prompt}

---

You are proposing a fix that a human CTO will cross-validate before it is applied. Nothing you output here will be auto-committed — your job is to produce a crisp, reviewable proposal. Prefer precision over comprehensiveness: a small change the CTO can verify by eye is more valuable than an ambitious multi-file patch.

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
  "verdict": "propose_fix" | "escalate",
  "confidence": 0.0,
  "spec_id": "HELLO-001 or null",
  "reasoning": "2-3 sentences: which spec the failure violates, why your fix restores it, and what you are uncertain about (the CTO will spend effort on the uncertain parts)",
  "fix": {{
    "file": "relative path from repo root",
    "before": "exact substring currently in the file (must match uniquely)",
    "after": "replacement text"
  }}
}}
```

If `verdict == "escalate"`, set `fix` to `null` and explain in `reasoning` what the CTO needs to look at.

Confidence guidance:
- 0.95+ : one-line change that exactly reverses the assertion failure
- 0.70-0.95 : small, clearly-motivated change
- < 0.70 : choose `escalate` instead

Prefer `before/after` spans that are a single line or a single token; avoid multi-line diffs when a one-liner works.
"""


def main(argv: list[str]) -> int:
    anth_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anth_key:
        log("error", "ANTHROPIC_API_KEY env var is empty")
        return 1

    cwd = pathlib.Path.cwd()
    try:
        failed = json.loads((cwd / "failed-jobs.json").read_text(encoding="utf-8"))
        ctx = json.loads((cwd / "fix-context.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log("error", f"cannot read input files: {e}")
        return 1

    jp_path = pathlib.Path(ctx.get("judgment_prompt", ""))
    judgment_prompt = (
        jp_path.read_text(encoding="utf-8")
        if jp_path.is_file()
        else "(judgment-prompt.md unavailable; do your best.)"
    )

    prompt = build_prompt(failed, ctx, judgment_prompt)

    log("notice", f"Calling Anthropic {ANTHROPIC_MODEL} for fix proposal")
    try:
        claude_text = call_anthropic(prompt, anth_key)
    except Exception as e:
        log("error", f"Anthropic call failed: {e}")
        # Still write a proposal file so the comment-poster step can say
        # "auto-fix crashed before producing a proposal" instead of silence.
        (cwd / "fix-proposal.json").write_text(
            json.dumps(
                {
                    "verdict": "escalate",
                    "confidence": 0.0,
                    "reasoning": f"Anthropic call failed: {e}",
                    "fix": None,
                    "raw_model_output": "",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1

    parsed = extract_json_block(claude_text)
    if parsed is None:
        log("warning", "Claude output was not parseable JSON; treating as escalate")
        parsed = {
            "verdict": "escalate",
            "confidence": 0.0,
            "reasoning": "Claude did not return a parseable JSON block.",
            "fix": None,
        }

    # Attach the raw model output for the CTO to cross-validate against.
    parsed["raw_model_output"] = claude_text[:8000]

    (cwd / "fix-proposal.json").write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    log(
        "notice",
        f"proposal written: verdict={parsed.get('verdict')}, "
        f"confidence={parsed.get('confidence')}, spec={parsed.get('spec_id')}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
