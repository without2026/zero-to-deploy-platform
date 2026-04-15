#!/usr/bin/env python3
"""Validate generated or auto-fixed files before pushing them.

Checks:
- changed YAML/JSON syntax
- changed Python syntax
- changed `.spec/*.spec.yml` entries against the schema
- duplicate spec IDs across the repo
"""
from __future__ import annotations

import json
import pathlib
import py_compile
import subprocess
import sys

try:
    import yaml
except ImportError:
    yaml = None

try:
    from jsonschema import Draft7Validator
except ImportError:
    Draft7Validator = None


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run(*args: str) -> list[str]:
    result = subprocess.run(
        list(args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _changed_files() -> list[pathlib.Path]:
    tracked = _run("git", "diff", "--name-only")
    staged = _run("git", "diff", "--cached", "--name-only")
    untracked = _run("git", "ls-files", "--others", "--exclude-standard")
    all_paths = {*(tracked or []), *(staged or []), *(untracked or [])}
    return sorted(REPO_ROOT / path for path in all_paths)


def _load_yaml(path: pathlib.Path):
    if yaml is None:
        raise RuntimeError("pyyaml is required")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _validate_data_files(paths: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix in {".yml", ".yaml"}:
                _load_yaml(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.relative_to(REPO_ROOT)}: parse error: {exc}")
    return errors


def _validate_python(paths: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.suffix != ".py" or not path.exists() or not path.is_file():
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{path.relative_to(REPO_ROOT)}: python syntax error: {exc.msg}")
    return errors


def _validate_specs(paths: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    spec_paths = [path for path in paths if path.name.endswith(".spec.yml")]
    if not spec_paths:
        return errors

    if Draft7Validator is None:
        errors.append("jsonschema is required to validate spec files")
        return errors

    schema_path = REPO_ROOT / ".pipeline" / "core" / "spec-schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)

    for path in spec_paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            data = _load_yaml(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.relative_to(REPO_ROOT)}: spec parse error: {exc}")
            continue

        for err in validator.iter_errors(data):
            where = ".".join(str(part) for part in err.absolute_path) or "<root>"
            errors.append(
                f"{path.relative_to(REPO_ROOT)}: schema error at {where}: {err.message}"
            )

    all_ids: dict[str, pathlib.Path] = {}
    spec_root = REPO_ROOT / ".spec"
    if spec_root.exists():
        for path in sorted(spec_root.glob("*.spec.yml")):
            try:
                data = _load_yaml(path) or {}
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.relative_to(REPO_ROOT)}: spec parse error: {exc}")
                continue
            for behavior in data.get("behaviors", []):
                spec_id = behavior.get("id")
                if not spec_id:
                    continue
                if spec_id in all_ids:
                    errors.append(
                        f"duplicate spec id {spec_id}: "
                        f"{all_ids[spec_id].relative_to(REPO_ROOT)} and {path.relative_to(REPO_ROOT)}"
                    )
                else:
                    all_ids[spec_id] = path

    return errors


def main() -> None:
    paths = _changed_files()
    if not paths:
        print("[INFO] No generated changes to validate")
        return

    errors: list[str] = []
    errors.extend(_validate_data_files(paths))
    errors.extend(_validate_python(paths))
    errors.extend(_validate_specs(paths))

    if errors:
        print("[BLOCK] generated change validation failed:")
        for err in errors:
            print(f"  {err}")
        sys.exit(1)

    print(f"[PASS] validated {len(paths)} changed file(s)")


if __name__ == "__main__":
    main()
