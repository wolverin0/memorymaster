#!/usr/bin/env bash
# =============================================================================
# MemoryMaster + OpenClaw Quick Installer
# =============================================================================
#
# One-liner:
#   curl -sSL https://raw.githubusercontent.com/wolverin0/memorymaster/main/scripts/openclaw-install.sh | bash
#
# What it does:
#   1. Checks Python 3.10+
#   2. Installs memorymaster with recommended extras
#   3. Initializes the database
#   4. Optionally sets up Obsidian vault export
#   5. Optionally installs a cron job for periodic maintenance
#
# Safe to run multiple times (idempotent).
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors (disabled if not a terminal)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' RED='' BLUE='' NC=''
fi

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ---------------------------------------------------------------------------
# 1. Check Python version
# ---------------------------------------------------------------------------
info "Checking Python version..."

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.10+ is required. Found: ${version:-none}. Install from https://www.python.org/downloads/"
fi

ok "Found $PYTHON $version"

# ---------------------------------------------------------------------------
# 2. Install memorymaster
# ---------------------------------------------------------------------------
info "Installing memorymaster with recommended extras..."

if "$PYTHON" -m pip show memorymaster &>/dev/null; then
    ok "memorymaster is already installed, upgrading..."
    "$PYTHON" -m pip install --upgrade "memorymaster[mcp,qdrant,security]" --quiet
else
    "$PYTHON" -m pip install "memorymaster[mcp,qdrant,security]" --quiet
fi

ok "memorymaster installed successfully"

# ---------------------------------------------------------------------------
# 3. Initialize database
# ---------------------------------------------------------------------------
DB_PATH="${MEMORYMASTER_DEFAULT_DB:-memorymaster.db}"

info "Initializing database at: $DB_PATH"

if [ -f "$DB_PATH" ]; then
    ok "Database already exists at $DB_PATH (skipping init)"
else
    memorymaster --db "$DB_PATH" init-db
    ok "Database initialized at $DB_PATH"
fi

# ---------------------------------------------------------------------------
# 4. Optional: Obsidian vault export
# ---------------------------------------------------------------------------
VAULT_DIR="${MEMORYMASTER_VAULT_DIR:-}"

if [ -n "$VAULT_DIR" ]; then
    info "Setting up Obsidian vault export to: $VAULT_DIR"
    mkdir -p "$VAULT_DIR"
    memorymaster --db "$DB_PATH" export-vault --output "$VAULT_DIR" --confirmed-only || true
    ok "Vault export configured at $VAULT_DIR"
else
    info "Skipping Obsidian vault setup (set MEMORYMASTER_VAULT_DIR to enable)"
fi

# ---------------------------------------------------------------------------
# 5. Optional: cron job for periodic maintenance
# ---------------------------------------------------------------------------
INSTALL_CRON="${MEMORYMASTER_INSTALL_CRON:-}"

if [ "$INSTALL_CRON" = "1" ] || [ "$INSTALL_CRON" = "true" ]; then
    CRON_ENTRY="0 */6 * * * $(command -v memorymaster) --db $DB_PATH run-cycle --quiet 2>/dev/null"

    if crontab -l 2>/dev/null | grep -qF "memorymaster.*run-cycle"; then
        ok "Cron job already exists (skipping)"
    else
        (crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -
        ok "Cron job installed: run-cycle every 6 hours"
    fi
else
    info "Skipping cron setup (set MEMORYMASTER_INSTALL_CRON=1 to enable)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}  MemoryMaster installed successfully!${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
echo "  Database: $DB_PATH"
echo "  Version:  $(memorymaster --version 2>/dev/null || echo 'unknown')"
echo ""
echo "  Quick start:"
echo "    memorymaster --db $DB_PATH ingest --text 'your claim' --source 'source://ref'"
echo "    memorymaster --db $DB_PATH query 'search terms'"
echo "    memorymaster --db $DB_PATH context 'topic' --budget 4000"
echo ""
echo "  MCP server:"
echo "    memorymaster-mcp"
echo ""
echo "  Dashboard:"
echo "    memorymaster --db $DB_PATH run-dashboard --port 8765"
echo ""
echo "  Full docs: https://github.com/wolverin0/memorymaster"
echo ""
