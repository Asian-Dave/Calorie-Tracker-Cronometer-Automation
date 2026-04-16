#!/usr/bin/env bash
# setup.sh — One-time credential setup for the calorie tracker.
#
# Stores your Cronometer credentials in the most secure location
# available for your platform:
#   macOS       → macOS Keychain
#   Linux (GUI) → Secret Service (GNOME Keyring / KWallet)
#   Linux (headless / Raspberry Pi) → .env file (chmod 600)
#
# Run once after cloning: ./setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "Cronometer Credential Setup"
echo "==========================="
echo ""

read -rp "Cronometer email:    " CRONO_USER
read -rsp "Cronometer password: " CRONO_PASS
echo ""

case "$(uname -s)" in

    Darwin)
        echo ""
        echo "macOS detected — storing in Keychain…"
        # -U updates if already exists
        security add-internet-password -U \
            -s "cronometer.com" \
            -a "$CRONO_USER" \
            -w "$CRONO_PASS" \
            -l "Cronometer (calorie-tracker)" 2>/dev/null || \
        security add-internet-password \
            -s "cronometer.com" \
            -a "$CRONO_USER" \
            -w "$CRONO_PASS" \
            -l "Cronometer (calorie-tracker)"
        echo -e "${GREEN}✓ Credentials saved to macOS Keychain.${NC}"
        echo "  You can view/edit them in Keychain Access → Passwords → cronometer.com"
        ;;

    Linux)
        if command -v secret-tool &>/dev/null; then
            echo ""
            echo "Linux with Secret Service detected — storing in system keyring…"
            # secret-tool needs username stored separately (it only stores one secret per label)
            # We store username as an attribute and password as the secret
            printf '%s' "$CRONO_PASS" | secret-tool store \
                --label="Cronometer (calorie-tracker)" \
                service "cronometer.com" \
                username "$CRONO_USER"
            # Also write username to a small non-secret file since secret-tool
            # can look up by attribute but reading back the username needs a lookup
            echo "$CRONO_USER" > "$SCRIPT_DIR/.crono_user"
            chmod 600 "$SCRIPT_DIR/.crono_user"
            echo -e "${GREEN}✓ Credentials saved to Secret Service keyring.${NC}"
        else
            echo ""
            echo -e "${YELLOW}No secret service found (headless Linux / Raspberry Pi).${NC}"
            echo "Storing in .env file with restricted permissions…"
            ENV_FILE="$SCRIPT_DIR/.env"
            cat > "$ENV_FILE" <<EOF
CRONOMETER_USER=$CRONO_USER
CRONOMETER_PASSWORD=$CRONO_PASS
EOF
            chmod 600 "$ENV_FILE"
            echo -e "${GREEN}✓ Credentials saved to .env (chmod 600).${NC}"
            echo "  Only your user account can read this file."
        fi
        ;;

    *)
        echo "Unknown platform — falling back to .env file…"
        ENV_FILE="$SCRIPT_DIR/.env"
        cat > "$ENV_FILE" <<EOF
CRONOMETER_USER=$CRONO_USER
CRONOMETER_PASSWORD=$CRONO_PASS
EOF
        chmod 600 "$ENV_FILE"
        echo -e "${GREEN}✓ Credentials saved to .env (chmod 600).${NC}"
        ;;
esac

echo ""
echo "Setup complete. You can now run ./add_meal.sh"
echo "The auth.json file is no longer needed (but still works as a fallback)."
echo ""
