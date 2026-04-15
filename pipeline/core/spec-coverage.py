#!/usr/bin/env python3
"""Platform-agnostic spec coverage checker.

Reads `.pipeline/platform.yml` to determine:
  - project_root: which subdirectory holds the project code
  - spec.dir: where .spec/*.spec.yml files live (default: .spec/ at repo root)
  - spec.comment_patterns / test_file_patterns: language-specific patterns

Test file patterns are resolved relative to {repo_root}/{project_root}/.

Exit 0 = pass, exit 2 = uncovered behaviors found.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if yaml:
        return yaml.safe_load(text) or {}
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"[ERROR] Cannot parse {path} without pyyaml")
        sys.exit(1)


def _find_repo_root() -> pathlib.Path:
    """Walk up from cwd to find a directory containing .pipeline/platform.yml."""
    p = pathlib.Path.cwd()
    for candidate in [p, *p.parents]:
        if (candidate / ".pipeline" / "platform.yml").exists():
            return candidate
    return p


def _load_spec_index(spec_dir: pathlib.Path) -> dict:
    """Load spec_index.yml if present; otherwise return {}."""
    index_path = spec_dir / "spec_index.yml"
    if not index_path.exists():
        return {}
    data = _load_yaml(index_path)
    return data if isinstance(data, dict) else {}


def _normalize_index_specs(index: dict) -> tuple[set[str], dict[str, set[str]]]:
    """Return (expected_spec_files, prefix_owners_by_file)."""
    expected: set[str] = set()
    owners: dict[str, set[str]] = {}
    specs = index.get("specs", {}) if isinstance(index, dict) else {}
    for _bucket in specs.values():
        if not isinstance(_bucket, list):
            continue
        for entry in _bucket:
            if not isinstance(entry, dict):
                continue
            f = entry.get("file")
            if not isinstance(f, str) or not f.strip():
                continue
            f = f.strip()
            expected.add(f)
            raw = entry.get("owns_prefixes", [])
            if isinstance(raw, list):
                owners[f] = {str(x).strip() for x in raw if str(x).strip()}
            else:
                owners[f] = set()
    return expected, owners


def _collect_spec_ids(spec_dir: pathlib.Path, index: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """Parse all .spec/*.spec.yml. Collect both `behaviors[]` and `invariants[]` —
    invariants are testable assertions too (a one-per-module module-level rule)
    and should require at least one test that references their ID, same as a
    behavior. Returns ({id: spec_entry}, {id: source_spec_file}).
    """
    behaviors: dict[str, dict] = {}
    sources: dict[str, str] = {}
    if not spec_dir.exists():
        return behaviors, sources

    for spec_file in sorted(spec_dir.glob("*.spec.yml")):
        data = _load_yaml(spec_file)
        # Behaviors and invariants share the same ID namespace and the same
        # test-coverage contract; collect both into the single map.
        for kind in ("behaviors", "invariants"):
            for entry in data.get(kind, []):
                bid = entry.get("id")
                if not bid:
                    continue
                if bid in behaviors:
                    prev = sources.get(bid, "(unknown)")
                    print(f"[BLOCK] Duplicate spec ID '{bid}' in {spec_file} (already defined in {prev})")
                    sys.exit(2)
                # Preserve the origin kind so downstream reporting can say
                # "behavior" vs "invariant" without re-parsing.
                enriched = dict(entry)
                enriched.setdefault("_kind", kind.rstrip("s"))
                behaviors[bid] = enriched
                sources[bid] = str(spec_file.relative_to(spec_dir.parent))

    _enforce_index_rules(spec_dir, index, behaviors, sources)
    return behaviors, sources


def _enforce_index_rules(
    spec_dir: pathlib.Path,
    index: dict,
    behaviors: dict[str, dict],
    sources: dict[str, str],
) -> None:
    """If spec_index.yml exists, enforce file topology + ID prefix ownership."""
    if not index:
        return

    expected_files, owners_by_file = _normalize_index_specs(index)
    if expected_files:
        actual_files = {
            str(p.relative_to(spec_dir.parent))
            for p in spec_dir.glob("*.spec.yml")
            if not p.name.startswith("_")
        }
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        if missing:
            print("[BLOCK] spec_index.yml lists missing spec file(s):")
            for f in missing:
                print(f"  {f}")
            sys.exit(2)
        if extra:
            print("[BLOCK] Found spec file(s) not listed in spec_index.yml:")
            for f in extra:
                print(f"  {f}")
            sys.exit(2)

    # Prefix policy (best-effort, only when owns_prefixes is provided)
    for bid, src in sources.items():
        # src is like ".spec/foo.spec.yml"
        allowed = owners_by_file.get(src)
        if not allowed:
            continue
        prefix = bid.split("-", 1)[0]
        if prefix not in allowed:
            print(f"[BLOCK] Spec ID prefix '{prefix}' is not allowed in {src}. Allowed: {sorted(allowed)} (id={bid})")
            sys.exit(2)


def _get_changed_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Return changed files for the current PR when running in CI."""
    base = None
    head = None

    event_path = pathlib.Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if event_path.exists():
        try:
            payload = json.loads(event_path.read_text(encoding="utf-8"))
            pr = payload.get("pull_request", {})
            base = pr.get("base", {}).get("sha")
            head = pr.get("head", {}).get("sha")
        except (json.JSONDecodeError, OSError, AttributeError):
            base = None
            head = None

    if not base:
        base = os.environ.get("BASE_SHA")
    if not head:
        head = os.environ.get("HEAD_SHA")

    if not base or not head:
        return []

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base, head],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return []

    changed: list[pathlib.Path] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            changed.append(repo_root / line)
    return changed


def _normalize_test_globs(value: object) -> list[str]:
    """Allow test_file_patterns per language to be a string or a list of strings (e.g. Kotlin unit + androidTest)."""
    if isinstance(value, list):
        return [str(x).strip() for x in value if x and str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _scan_with_glob(
    project_dir: pathlib.Path,
    comment_patterns: dict[str, str],
    test_file_patterns: dict[str, object],
) -> set[str]:
    """Glob-based scan for spec: ID comments in test files under project_dir."""
    covered: set[str] = set()

    for lang, comment_prefix in comment_patterns.items():
        if not comment_prefix or not str(comment_prefix).strip():
            continue
        globs = _normalize_test_globs(test_file_patterns.get(lang, ""))
        if not globs:
            continue
        escaped = re.escape(str(comment_prefix).strip())
        # Spec IDs are hyphenated tokens of 2 or more parts. Common shapes:
        #   2-part: HELLO-001, AUTH-LOGIN
        #   3-part: HELLO-INV-001, AUTH-LOGIN-001
        #   4-part: TODO-SHARE-CONCURRENT-001
        # The prior regex captured only the first two parts, silently
        # truncating longer IDs and producing "non-existent spec" warnings
        # for correctly-referenced invariants.
        pattern = re.compile(rf"{escaped}\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)")

        for file_glob in globs:
            for test_path in project_dir.glob(file_glob):
                if not test_path.is_file():
                    continue
                try:
                    text = test_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for line in text.splitlines():
                    m = pattern.search(line)
                    if m:
                        covered.add(m.group(1))

    return covered


def _compile_comment_patterns(comment_patterns: dict[str, str]) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for prefix in comment_patterns.values():
        escaped = re.escape(prefix.strip())
        # Same N-part-ID regex as _scan_with_glob (see note there).
        patterns.append(re.compile(rf"{escaped}\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)"))
    return patterns


def _scan_file_for_spec_ids(
    path: pathlib.Path,
    patterns: list[re.Pattern[str]],
) -> set[str]:
    refs: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return refs

    for line in text.splitlines():
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                refs.add(match.group(1))
    return refs


def main() -> None:
    repo_root = _find_repo_root()
    config_path = repo_root / ".pipeline" / "platform.yml"

    if not config_path.exists():
        print("[WARN] .pipeline/platform.yml not found, skipping spec coverage check")
        sys.exit(0)

    config = _load_yaml(config_path)

    project_root_rel = config.get("project_root", ".")
    project_dir = repo_root / project_root_rel

    spec_config = config.get("spec", {})
    comment_patterns = spec_config.get("comment_patterns", {})
    test_file_patterns = spec_config.get("test_file_patterns", {})

    if not comment_patterns:
        print("[WARN] No spec.comment_patterns in platform.yml, skipping")
        sys.exit(0)

    spec_dir_rel = spec_config.get("dir", ".spec")
    spec_dir = repo_root / spec_dir_rel

    if not project_dir.exists():
        print(f"[WARN] project_root '{project_root_rel}' does not exist, skipping")
        sys.exit(0)

    spec_index = _load_spec_index(spec_dir)
    behaviors, _sources = _collect_spec_ids(spec_dir, spec_index)
    covered = _scan_with_glob(project_dir, comment_patterns, test_file_patterns)
    comment_regexes = _compile_comment_patterns(comment_patterns)
    changed_files = _get_changed_files(repo_root)
    changed_under_project = [
        p for p in changed_files
        if p.exists() and project_dir in [p, *p.parents]
    ]
    changed_spec_files = [
        p for p in changed_files
        if p.exists() and spec_dir in [p, *p.parents]
    ]

    changed_test_files: list[pathlib.Path] = []
    test_file_set: set[pathlib.Path] = set()
    for _lang, raw_patterns in test_file_patterns.items():
        for glob_pattern in _normalize_test_globs(raw_patterns):
            test_file_set.update(project_dir.glob(glob_pattern))
    for path in changed_under_project:
        if path in test_file_set:
            changed_test_files.append(path)

    changed_source_files = [
        p for p in changed_under_project
        if p.is_file()
        and p not in test_file_set
        and p.suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".kt", ".java", ".swift"}
    ]

    all_ids = set(behaviors.keys())
    uncovered = all_ids - covered
    orphan_refs = covered - all_ids
    changed_test_refs: dict[pathlib.Path, set[str]] = {
        path: _scan_file_for_spec_ids(path, comment_regexes)
        for path in changed_test_files
    }
    changed_tests_missing_refs = [
        path for path, refs in changed_test_refs.items() if not refs
    ]
    changed_orphan_refs = sorted({
        ref for refs in changed_test_refs.values() for ref in refs if ref not in all_ids
    })

    print(f"[INFO] project_root={project_root_rel}, spec_dir={spec_dir_rel}")
    print(f"[INFO] Spec behaviors: {len(all_ids)}, Covered: {len(covered)}, Uncovered: {len(uncovered)}")
    if changed_files:
        print(
            f"[INFO] PR-scoped enforcement: changed_source={len(changed_source_files)}, "
            f"changed_tests={len(changed_test_files)}, changed_specs={len(changed_spec_files)}"
        )

    if not behaviors:
        if changed_source_files or changed_test_files or changed_spec_files:
            print("[BLOCK] No behavior specs defined, but this PR changes source/test/spec files:")
            for path in [*changed_source_files, *changed_test_files, *changed_spec_files]:
                print(f"  {path.relative_to(repo_root)}")
            sys.exit(2)
        print("[INFO] No behaviors defined in spec dir, nothing to check")
        sys.exit(0)

    if orphan_refs:
        print(f"[WARN] {len(orphan_refs)} test(s) reference non-existent spec IDs: {sorted(orphan_refs)}")

    if changed_tests_missing_refs:
        print("[BLOCK] Changed test file(s) missing a spec reference:")
        for path in changed_tests_missing_refs:
            print(f"  {path.relative_to(repo_root)}")
        sys.exit(2)

    if changed_orphan_refs:
        print(f"[BLOCK] Changed test file(s) reference non-existent spec IDs: {changed_orphan_refs}")
        sys.exit(2)

    if changed_source_files and not changed_spec_files and not changed_test_files:
        print("[BLOCK] Source files changed without any corresponding spec or test updates:")
        for path in changed_source_files:
            print(f"  {path.relative_to(repo_root)}")
        sys.exit(2)

    if changed_source_files and not all_ids:
        print("[BLOCK] Source files changed but no behavior specs exist yet for this project.")
        for path in changed_source_files:
            print(f"  {path.relative_to(repo_root)}")
        sys.exit(2)

    if uncovered:
        print(f"[BLOCK] {len(uncovered)} uncovered behavior(s):")
        for uid in sorted(uncovered):
            b = behaviors[uid]
            print(f"  {uid}: {b.get('description', '(no description)')}")
        sys.exit(2)

    print("[PASS] All spec behaviors have at least one test")


if __name__ == "__main__":
    main()
