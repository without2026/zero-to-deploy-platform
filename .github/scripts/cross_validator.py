#!/usr/bin/env python3
"""Cross-validate Claude and Codex code review outputs.

Phase 1: Match issues from independent reviews by (file, line, category).
Phase 2 (Targeted Validation): For issues found by only one model,
  read the other model's targeted confirmation from Phase 2 artifacts.

Exit 0 = pass, exit 2 = blocked by CRITICAL issues.

Inputs (artifact directories from CI):
  claude-review/claude-review.json
  codex-review/codex-review.json
  targeted-validation/targeted-validation.json  (optional, Phase 2)

Output:
  cross-validated.json
"""
from __future__ import annotations

import json
import pathlib
import sys
from difflib import SequenceMatcher

# --- Thresholds ---
# For a single-model finding to pass, cross_confidence must meet these.
THRESHOLDS = {
    "CRITICAL": 0.50,
    "HIGH": 0.65,
    "MEDIUM": 0.80,
    "LOW": 0.90,
}

# --- Phase 1: Independent matching ---

def _normalize(issue: dict) -> str:
    return f"{issue['file']}:{issue['line_start']}:{issue['category']}"


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _phase1_match(
    claude_issues: list[dict],
    codex_issues: list[dict],
) -> list[dict]:
    """Match issues across models. Returns scored list."""
    results: list[dict] = []
    matched_codex_keys: set[str] = set()

    for c in claude_issues:
        key = _normalize(c)
        candidates = [x for x in codex_issues if _normalize(x) == key]

        if not candidates:
            score = c["confidence"] * 0.6
            results.append({
                **c,
                "cross_confidence": round(score, 3),
                "agreement": "claude_only",
                "original_confidence": c["confidence"],
            })
        else:
            best = max(candidates, key=lambda x: _similarity(c["description"], x["description"]))
            sim = _similarity(c["description"], best["description"])
            matched_codex_keys.add(_normalize(best))

            if sim >= 0.6:
                score = min(1.0, (c["confidence"] + best["confidence"]) / 2 * 1.2)
                agreement = "both_agree"
            else:
                score = (c["confidence"] + best["confidence"]) / 2 * 0.8
                agreement = "partial_match"

            results.append({
                **c,
                "cross_confidence": round(score, 3),
                "agreement": agreement,
                "original_confidence": c["confidence"],
            })

    for x in codex_issues:
        if _normalize(x) not in matched_codex_keys:
            score = x["confidence"] * 0.6
            results.append({
                **x,
                "cross_confidence": round(score, 3),
                "agreement": "codex_only",
                "original_confidence": x["confidence"],
            })

    return results


# --- Phase 2: Targeted Validation ---

def _phase2_apply(
    scored: list[dict],
    validations: list[dict],
) -> list[dict]:
    """Apply targeted validation results to single-model findings.

    Each validation entry:
      {
        "file": "...", "line_start": N, "category": "...",
        "original_finder": "claude" | "codex",
        "verdict": "confirmed" | "denied" | "uncertain"
      }
    """
    val_map: dict[str, dict] = {}
    for v in validations:
        key = f"{v['file']}:{v['line_start']}:{v['category']}"
        val_map[key] = v

    for issue in scored:
        if issue["agreement"] not in ("claude_only", "codex_only"):
            continue

        key = _normalize(issue)
        val = val_map.get(key)
        if val is None:
            continue

        orig_conf = issue["original_confidence"]
        verdict = val.get("verdict", "uncertain")

        if verdict == "confirmed":
            issue["cross_confidence"] = round(orig_conf, 3)
            issue["agreement"] = "cross_confirmed"
        elif verdict == "denied":
            issue["cross_confidence"] = round(orig_conf * 0.4, 3)
            issue["agreement"] = "cross_denied"
        # "uncertain" -> keep Phase 1 score (original * 0.6)

    return scored


# --- Policy gate ---

def _gate(scored: list[dict]) -> tuple[list[dict], list[dict]]:
    blocks: list[dict] = []
    warns: list[dict] = []
    for issue in scored:
        threshold = THRESHOLDS.get(issue["severity"])
        if threshold is None:
            continue
        if issue["cross_confidence"] >= threshold:
            if issue["severity"] == "CRITICAL":
                blocks.append(issue)
            else:
                warns.append(issue)
        elif issue["severity"] == "CRITICAL":
            issue["gate_note"] = "below_threshold_but_critical"
            warns.append(issue)
    return blocks, warns


# --- Main ---

def main() -> None:
    claude_path = pathlib.Path("claude-review/claude-review.json")
    codex_path = pathlib.Path("codex-review/codex-review.json")
    targeted_path = pathlib.Path("targeted-validation/targeted-validation.json")

    if not claude_path.exists() and not codex_path.exists():
        print("[ERROR] Neither claude-review.json nor codex-review.json found")
        sys.exit(1)

    claude_issues: list[dict] = []
    codex_issues: list[dict] = []

    if claude_path.exists():
        try:
            claude_issues = json.loads(claude_path.read_text()).get("issues", [])
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"[WARN] Failed to parse claude-review.json: {e}")

    if codex_path.exists():
        try:
            codex_issues = json.loads(codex_path.read_text()).get("issues", [])
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"[WARN] Failed to parse codex-review.json: {e}")

    # Fallback: if only one model produced results, use them directly
    if not claude_issues and not codex_issues:
        print("[PASS] No issues found by either model")
        pathlib.Path("cross-validated.json").write_text(
            json.dumps({"blocks": [], "warns": [], "all": []}, indent=2)
        )
        sys.exit(0)

    scored = _phase1_match(claude_issues, codex_issues)

    if targeted_path.exists():
        try:
            validations = json.loads(targeted_path.read_text())
            if isinstance(validations, list):
                scored = _phase2_apply(scored, validations)
                print(f"[INFO] Phase 2 targeted validation applied ({len(validations)} items)")
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"[WARN] Failed to parse targeted-validation.json: {e}")

    blocks, warns = _gate(scored)

    output = {"blocks": blocks, "warns": warns, "all": scored}
    pathlib.Path("cross-validated.json").write_text(json.dumps(output, indent=2))

    if blocks:
        print(f"[BLOCK] {len(blocks)} critical issue(s) found")
        for b in blocks:
            print(
                f"  {b['severity']} ({b['agreement']}, {b['cross_confidence']:.2f}): "
                f"{b['file']}:{b['line_start']} — {b['description']}"
            )
        sys.exit(2)

    if warns:
        print(f"[WARN] {len(warns)} warning(s), 0 blocks")
        for w in warns:
            print(
                f"  {w['severity']} ({w['agreement']}, {w['cross_confidence']:.2f}): "
                f"{w['file']}:{w['line_start']} — {w['description']}"
            )
    else:
        print("[PASS] 0 blocks, 0 warnings")


if __name__ == "__main__":
    main()
