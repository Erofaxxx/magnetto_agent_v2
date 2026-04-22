#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — One-shot installation for ClickHouse Analytics Agent
# Run as root on Ubuntu 22.04 / 24.04
# Usage:  chmod +x setup.sh && sudo bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="/root/clickhouse_analytics_agent"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_NAME="analytics-agent"

echo "===================================================="
echo "  ClickHouse Analytics Agent — Setup"
echo "===================================================="

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl wget git

# ── 2. Create project directory ───────────────────────────────────────────────
echo "[2/7] Setting up project directory..."
mkdir -p "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/temp_data"

# ── 3. Python virtual environment ─────────────────────────────────────────────
echo "[3/7] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "      Upgrading pip..."
pip install --upgrade pip -q

echo "      Installing Python dependencies..."
pip install -r "$PROJECT_DIR/requirements.txt" -q

echo "      Python version: $(python --version)"
echo "      LangGraph version: $(pip show langgraph | grep Version)"

# ── 4. SSL certificate for Yandex Cloud ClickHouse ───────────────────────────
echo "[4/7] Downloading Yandex Cloud SSL certificate..."
if [ ! -f "$PROJECT_DIR/YandexInternalRootCA.crt" ]; then
    curl -s "https://storage.yandexcloud.net/cloud-certs/CA.pem" \
         -o "$PROJECT_DIR/YandexInternalRootCA.crt"
    echo "      Saved: $PROJECT_DIR/YandexInternalRootCA.crt"
else
    echo "      Already exists: $PROJECT_DIR/YandexInternalRootCA.crt"
fi

# ── 5. .env file ──────────────────────────────────────────────────────────────
echo "[5/7] Setting up .env..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo "  ⚠️  IMPORTANT: Edit $PROJECT_DIR/.env and fill in:"
    echo "      - OPENROUTER_API_KEY"
    echo "      - CLICKHOUSE_HOST / USER / PASSWORD / DATABASE"
    echo ""
else
    echo "      .env already exists — skipping"
fi

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "[6/7] Installing systemd service..."
cp "$PROJECT_DIR/agent.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "      Service $SERVICE_NAME enabled"

# ── 7. Nginx configuration ────────────────────────────────────────────────────
echo "[7/7] Installing Nginx config..."
cp "$PROJECT_DIR/nginx.conf" "/etc/nginx/sites-available/$SERVICE_NAME"
ln -sf "/etc/nginx/sites-available/$SERVICE_NAME" "/etc/nginx/sites-enabled/$SERVICE_NAME"
nginx -t && systemctl reload nginx
echo "      Nginx configured"

echo ""
echo "===================================================="
echo "  ✅ Setup complete!"
echo "===================================================="
echo ""
echo "  Next steps:"
echo "  1. Edit .env:          nano $PROJECT_DIR/.env"
echo "  2. Start service:      systemctl start $SERVICE_NAME"
echo "  3. Check status:       systemctl status $SERVICE_NAME"
echo "  4. View logs:          journalctl -u $SERVICE_NAME -f"
echo "  5. Get HTTPS cert:     certbot --nginx -d server.asktab.ru"
echo "  6. Test API:           curl https://server.asktab.ru/health"
echo ""
