#!/usr/bin/env python3
"""Platform-agnostic assert guard.

Detects weakened or bypassed test assertions across all languages.

Reads `.pipeline/platform.yml` for:
  - project_root: where test files live
  - ci.adapter: which adapter's anti-patterns.yml to load

Strategy:
1. Load the adapter's anti-patterns.yml for regex-based checks (all languages).
2. For Python test files, additionally perform AST-level checks.

Exit 0 = clean, exit 2 = blocked.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: pathlib.Path) -> dict | list:
    text = path.read_text(encoding="utf-8")
    if yaml:
        return yaml.safe_load(text) or {}
    import json
    return json.loads(text)


def _find_repo_root() -> pathlib.Path:
    p = pathlib.Path.cwd()
    for candidate in [p, *p.parents]:
        if (candidate / ".pipeline" / "platform.yml").exists():
            return candidate
    return p


def _python_ast_checks(path: pathlib.Path) -> list[tuple[int, str]]:
    """AST-level checks specific to Python test files."""
    issues: list[tuple[int, str]] = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, OSError):
        return issues

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                issues.append((node.lineno, "assert True bypass"))

        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name in ("skip", "skipTest", "skipIf", "skipUnless"):
                issues.append((getattr(node, "lineno", 0), f"{name}() call"))

    return issues


def _regex_checks(
    path: pathlib.Path,
    anti_patterns: list[dict],
) -> list[tuple[int, str]]:
    """Check a file against regex anti-patterns from adapter config."""
    issues: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return issues

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        for ap in anti_patterns:
            pat = ap.get("pattern", "")
            if not pat:
                continue
            if re.search(re.escape(pat), stripped):
                issues.append((i, ap.get("description", f"matches: {pat}")))

    return issues


def _load_anti_patterns(repo_root: pathlib.Path, adapter: str) -> list[dict]:
    """Load anti-patterns.yml from the adapter directory."""
    ap_path = repo_root / ".pipeline" / "adapters" / adapter / "anti-patterns.yml"
    if not ap_path.exists():
        return []
    data = _load_yaml(ap_path)
    return data.get("test_anti_patterns", []) if isinstance(data, dict) else []


def _normalize_test_globs(value: object) -> list[str]:
    """Each language entry may be a single glob or a list (e.g. Kotlin unit + androidTest)."""
    if isinstance(value, list):
        return [str(x).strip() for x in value if x and str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _get_test_globs(config: dict) -> list[str]:
    """Get test file patterns from platform.yml."""
    spec = config.get("spec", {})
    patterns = spec.get("test_file_patterns", {})
    if not patterns:
        return ["tests/**/test_*.py"]
    out: list[str] = []
    for raw in patterns.values():
        out.extend(_normalize_test_globs(raw))
    return out or ["tests/**/test_*.py"]


def main() -> None:
    repo_root = _find_repo_root()
    config_path = repo_root / ".pipeline" / "platform.yml"

    config: dict = {}
    adapter = "electron"
    project_root_rel = "."

    if config_path.exists():
        config = _load_yaml(config_path)
        adapter = config.get("ci", {}).get("adapter", "electron")
        project_root_rel = config.get("project_root", ".")

    project_dir = repo_root / project_root_rel
    anti_patterns = _load_anti_patterns(repo_root, adapter)
    test_globs = _get_test_globs(config)

    all_errors: list[str] = []

    test_files: set[pathlib.Path] = set()
    for tg in test_globs:
        test_files.update(project_dir.glob(tg))

    for f in sorted(test_files):
        if not f.is_file():
            continue

        issues: list[tuple[int, str]] = []

        if f.suffix == ".py":
            issues.extend(_python_ast_checks(f))

        if anti_patterns:
            issues.extend(_regex_checks(f, anti_patterns))

        for lineno, msg in issues:
            try:
                rel = f.relative_to(repo_root)
            except ValueError:
                rel = f
            all_errors.append(f"  {rel}:{lineno}: {msg}")

    if all_errors:
        print(f"[BLOCK] assert guard found {len(all_errors)} issue(s):")
        for e in all_errors:
            print(e)
        sys.exit(2)

    print("[PASS] assert guard clean")


if __name__ == "__main__":
    main()
