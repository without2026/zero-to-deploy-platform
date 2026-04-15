#!/usr/bin/env bash
# Android performance check: APK/AAB size limit.
#
# CWD contract: repo root. Searches under ${PROJECT_ROOT:-app}/build/outputs/
# for AAB/APK artifacts. Honors PROJECT_ROOT env var so consumers with
# non-standard layouts (legacy, monorepo) work without editing this script.
#
# set -uo pipefail  (NOT -e — we do not want find-missing-dir to abort.)

set -uo pipefail

MAX_AAB_MB=50
MAX_APK_MB=100
ROOT="${PROJECT_ROOT:-app}"

check_size() {
  local file="$1"
  local max_mb="$2"
  local label="$3"

  if [ ! -f "$file" ]; then
    echo "[SKIP] $label not found: $file"
    return 0
  fi

  local size_bytes
  size_bytes=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file")
  local size_mb=$((size_bytes / 1048576))

  if [ "$size_mb" -gt "$max_mb" ]; then
    echo "[BLOCK] $label too large: ${size_mb}MB (limit: ${max_mb}MB)"
    exit 2
  fi

  echo "[PASS] $label: ${size_mb}MB (limit: ${max_mb}MB)"
}

# If the bundle/apk output dirs don't exist yet (no build ran), skip cleanly.
# find returns exit 1 with "No such file or directory" when path missing;
# capture both cases.
AAB_FILE=""
APK_FILE=""
if [ -d "${ROOT}/build/outputs/bundle" ]; then
  AAB_FILE=$(find "${ROOT}/build/outputs/bundle" -name "*.aab" 2>/dev/null | head -1)
fi
if [ -d "${ROOT}/build/outputs/apk" ]; then
  APK_FILE=$(find "${ROOT}/build/outputs/apk" -name "*.apk" 2>/dev/null | head -1)
fi

if [ -n "$AAB_FILE" ]; then
  check_size "$AAB_FILE" "$MAX_AAB_MB" "AAB"
fi

if [ -n "$APK_FILE" ]; then
  check_size "$APK_FILE" "$MAX_APK_MB" "APK"
fi

if [ -z "$AAB_FILE" ] && [ -z "$APK_FILE" ]; then
  echo "[SKIP] No AAB/APK found under ${ROOT}/build/outputs/ — skipping size check"
fi
