#!/usr/bin/env bash
# Android performance check: APK/AAB size limit
set -euo pipefail

MAX_AAB_MB=50
MAX_APK_MB=100

check_size() {
  local file="$1"
  local max_mb="$2"
  local label="$3"

  if [ ! -f "$file" ]; then
    echo "[SKIP] $label not found: $file"
    return 0
  fi

  size_bytes=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file")
  size_mb=$((size_bytes / 1048576))

  if [ "$size_mb" -gt "$max_mb" ]; then
    echo "[BLOCK] $label too large: ${size_mb}MB (limit: ${max_mb}MB)"
    exit 2
  fi

  echo "[PASS] $label: ${size_mb}MB (limit: ${max_mb}MB)"
}

AAB_FILE=$(find app/build/outputs/bundle -name "*.aab" 2>/dev/null | head -1)
APK_FILE=$(find app/build/outputs/apk -name "*.apk" 2>/dev/null | head -1)

if [ -n "$AAB_FILE" ]; then
  check_size "$AAB_FILE" "$MAX_AAB_MB" "AAB"
fi

if [ -n "$APK_FILE" ]; then
  check_size "$APK_FILE" "$MAX_APK_MB" "APK"
fi

if [ -z "$AAB_FILE" ] && [ -z "$APK_FILE" ]; then
  echo "[SKIP] No AAB/APK found — skipping size check"
fi
