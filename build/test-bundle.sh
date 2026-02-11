#!/bin/bash

# Runtime test script for OnionPress bundle
# Tests both structure and execution of bundled binaries.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$PROJECT_DIR/OnionPress.app"
BIN_DIR="$APP_PATH/Contents/Resources/bin"

PASS=0
FAIL=0
SKIP=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }

echo "Testing OnionPress bundle..."
echo ""

# --- Structure checks ---
echo "=== Structure ==="

UNIVERSAL_BINARIES=("colima" "limactl" "docker" "docker-compose")

for binary in "${UNIVERSAL_BINARIES[@]}"; do
    # Exists and executable
    if [ -f "$BIN_DIR/$binary" ] && [ -x "$BIN_DIR/$binary" ]; then
        pass "$binary exists and is executable"
    else
        fail "$binary missing or not executable"
        continue
    fi

    # Is Mach-O (not a shell script)
    if file "$BIN_DIR/$binary" | grep -q "Mach-O"; then
        pass "$binary is Mach-O binary"
    else
        fail "$binary is not a Mach-O binary ($(file "$BIN_DIR/$binary"))"
        continue
    fi

    # Is universal (arm64 + x86_64)
    ARCHS=$(lipo -archs "$BIN_DIR/$binary" 2>/dev/null || echo "unknown")
    if [[ "$ARCHS" == *"arm64"* ]] && [[ "$ARCHS" == *"x86_64"* ]]; then
        pass "$binary is universal ($ARCHS)"
    else
        fail "$binary is not universal (found: $ARCHS)"
    fi

    # Is signed
    if codesign -v "$BIN_DIR/$binary" 2>/dev/null; then
        pass "$binary is signed"
    else
        fail "$binary is not signed"
    fi
done

# lima wrapper script
if [ -f "$BIN_DIR/lima" ] && [ -x "$BIN_DIR/lima" ] && file "$BIN_DIR/lima" | grep -q "text"; then
    pass "lima wrapper script exists"
else
    fail "lima wrapper script missing or wrong type"
fi

# mkp224o (optional, native only)
if [ -f "$BIN_DIR/mkp224o" ]; then
    if [ -x "$BIN_DIR/mkp224o" ]; then
        pass "mkp224o exists and is executable"
    else
        fail "mkp224o exists but not executable"
    fi
else
    skip "mkp224o not present (optional)"
fi

echo ""

# --- ARM64 execution ---
echo "=== ARM64 Execution ==="

if sysctl hw.optional.arm64 2>/dev/null | grep -q ": 1"; then
    # We're on Apple Silicon — can test arm64 natively
    if arch -arm64 "$BIN_DIR/colima" version >/dev/null 2>&1; then
        pass "colima runs (arm64)"
    else
        fail "colima failed to run (arm64)"
    fi

    if arch -arm64 "$BIN_DIR/limactl" --version >/dev/null 2>&1; then
        pass "limactl runs (arm64)"
    else
        fail "limactl failed to run (arm64)"
    fi

    if arch -arm64 "$BIN_DIR/docker" --version >/dev/null 2>&1; then
        pass "docker runs (arm64)"
    else
        fail "docker failed to run (arm64)"
    fi

    if arch -arm64 "$BIN_DIR/docker-compose" version >/dev/null 2>&1; then
        pass "docker-compose runs (arm64)"
    else
        fail "docker-compose failed to run (arm64)"
    fi
else
    skip "Not on Apple Silicon — cannot test arm64 execution"
fi

echo ""

# --- x86_64 execution (requires Rosetta) ---
echo "=== x86_64 Execution ==="

if arch -x86_64 /usr/bin/true 2>/dev/null; then
    # Rosetta is installed — can test x86_64
    if arch -x86_64 "$BIN_DIR/colima" version >/dev/null 2>&1; then
        pass "colima runs (x86_64)"
    else
        fail "colima failed to run (x86_64)"
    fi

    if arch -x86_64 "$BIN_DIR/limactl" --version >/dev/null 2>&1; then
        pass "limactl runs (x86_64)"
    else
        fail "limactl failed to run (x86_64)"
    fi

    if arch -x86_64 "$BIN_DIR/docker" --version >/dev/null 2>&1; then
        pass "docker runs (x86_64)"
    else
        fail "docker failed to run (x86_64)"
    fi

    if arch -x86_64 "$BIN_DIR/docker-compose" version >/dev/null 2>&1; then
        pass "docker-compose runs (x86_64)"
    else
        fail "docker-compose failed to run (x86_64)"
    fi
else
    skip "Rosetta not installed — cannot test x86_64 execution"
fi

echo ""

# --- Rosetta trigger scan ---
echo "=== Rosetta Trigger Scan ==="
echo "  Scanning entire .app bundle for x86_64-only Mach-O files..."

TRIGGERS=0
while IFS= read -r -d '' f; do
    # Quick check: skip if file command doesn't mention Mach-O
    FILE_TYPE=$(file "$f")
    if echo "$FILE_TYPE" | grep -q "Mach-O"; then
        ARCHS=$(lipo -archs "$f" 2>/dev/null || echo "unknown")
        if [[ "$ARCHS" == "x86_64" ]]; then
            fail "x86_64-only Mach-O: ${f#$APP_PATH/}"
            TRIGGERS=$((TRIGGERS + 1))
        fi
    fi
done < <(find "$APP_PATH" -type f -print0)

if [ $TRIGGERS -eq 0 ]; then
    pass "No x86_64-only Mach-O files in bundle"
fi

echo ""

# --- Lima guest agents ---
echo "=== Lima Guest Agents ==="

SHARE_DIR="$APP_PATH/Contents/Resources/share/lima"
if [ -d "$SHARE_DIR" ]; then
    AARCH64_AGENTS=$(find "$SHARE_DIR" -name "*aarch64*" -o -name "*arm64*" 2>/dev/null | wc -l | tr -d ' ')
    X86_64_AGENTS=$(find "$SHARE_DIR" -name "*x86_64*" -o -name "*amd64*" 2>/dev/null | wc -l | tr -d ' ')

    if [ "$AARCH64_AGENTS" -gt 0 ]; then
        pass "aarch64 guest agent files found ($AARCH64_AGENTS files)"
    else
        fail "No aarch64 guest agent files found"
    fi

    if [ "$X86_64_AGENTS" -gt 0 ]; then
        pass "x86_64 guest agent files found ($X86_64_AGENTS files)"
    else
        fail "No x86_64 guest agent files found"
    fi
else
    fail "Lima share directory not found"
fi

echo ""

# --- Docker-compose plugin ---
echo "=== Docker Compose Plugin ==="

if file "$BIN_DIR/docker-compose" | grep -q "Mach-O"; then
    pass "docker-compose is a Mach-O binary (not a shell script)"
else
    fail "docker-compose is not a Mach-O binary"
fi

echo ""

# --- Summary ---
TOTAL=$((PASS + FAIL + SKIP))
echo "==============================="
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped (total $TOTAL)"
echo "==============================="

if [ $FAIL -gt 0 ]; then
    echo "❌ Some tests failed!"
    exit 1
else
    echo "✅ All tests passed!"
    exit 0
fi
