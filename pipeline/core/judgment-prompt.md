# 5-Step Judgment Prompt Template

This prompt is used by `ci-auto-fix.yml` to diagnose whether a failing test
indicates a code bug or a test bug. The CI workflow fills in the `{...}` placeholders.

---

## Context

- Platform: {platform}
- Failing test: {test_source}
- Test language: {test_language}
- Spec ID: {spec_id}
- Spec type: {spec_type}
- Spec behavior: {spec_description}
- Spec expected: {spec_expected}
- Spec source authority: {spec_source}
- Production code: {prod_code}
- PR diff: {pr_diff}
- Passing sibling tests: {siblings}

## 5-Step Judgment

### Step 1: Convert assertion to natural-language spec

Restate what this test asserts in plain language:
  "{assertion}" -> "{natural_language}"

### Step 2: Trace spec origin

Spec ID = {spec_id}, source = {spec_source}.

Apply authority modifier from `.pipeline/core/authority.yml`:
- If source = "phase2-acceptance" -> CTO-approved acceptance criteria. Base confidence.
- If source = "incident-regression" -> Real production incident. confidence += 0.2.
- If source = "regression-guard" -> Spec itself may be outdated. confidence -= 0.15.
- If no spec ID found -> origin unknown. confidence -= 0.30.

Apply type adjustment:
- If spec.type = "lifecycle" -> Timing-sensitive, may be flaky. confidence -= 0.10.
- If spec.type = "gesture" -> Emulator instability. confidence -= 0.10.
- If spec.type = "permission" -> OS-version dependent. confidence -= 0.05.

### Step 3: Three-way comparison

(a) spec.expected == assertion? (Is the test written per spec?)
(b) code behavior == spec.expected? (Does the code behave per spec?)

- If (a)=yes, (b)=no -> fix_code (code violates spec)
- If (a)=no, (b)=yes -> fix_test (test misrepresents spec)
- If (a)=no, (b)=no -> both wrong, escalate

### Step 4: Generate minimal fix candidate

For the chosen direction, produce the smallest correct change.
Evaluate: regression risk, blast radius, consistency with sibling tests.

### Step 5: Output

```json
{
  "verdict": "fix_code | fix_test | escalate",
  "confidence": 0.0-1.0,
  "reasoning": "...",
  "spec_id": "...",
  "spec_type": "...",
  "fix_diff": "..."
}
```

## Confidence thresholds

- `fix_code`: confidence >= 0.7 -> auto-apply
- `fix_test`: confidence >= 0.8 -> auto-apply
- Below threshold -> escalate with `needs-human` label
