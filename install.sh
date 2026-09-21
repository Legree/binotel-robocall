#!/usr/bin/env bash
# Ubuntu 22.04/24.04. Запускати від root на чистому VPS.
set -e
apt-get update
apt-get install -y asterisk ffmpeg python3-venv python3-pip

# конфіги Asterisk
cp asterisk/pjsip.conf /etc/asterisk/pjsip.conf
cp asterisk/extensions.conf /etc/asterisk/extensions.conf
cp asterisk/manager.conf /etc/asterisk/manager.conf
cp asterisk/rtp.conf /etc/asterisk/rtp.conf
mkdir -p /var/lib/asterisk/sounds/robocall
chown -R asterisk:asterisk /var/lib/asterisk/sounds/robocall
chmod 775 /var/lib/asterisk/sounds/robocall
systemctl enable asterisk && systemctl restart asterisk

# бот
python3 -m venv /opt/robocall-venv
/opt/robocall-venv/bin/pip install -r requirements.txt
mkdir -p /opt/robocall && cp bot.py travelon.py telegram_bot.py /opt/robocall/ && cp .env.example /opt/robocall/.env && chmod 600 /opt/robocall/.env
usermod -aG asterisk root

cat > /etc/systemd/system/robocall.service << 'UNIT'
[Unit]
Description=Binotel robocall bot
After=network.target asterisk.service

[Service]
WorkingDirectory=/opt/robocall
EnvironmentFile=/opt/robocall/.env
ExecStart=/opt/robocall-venv/bin/uvicorn bot:app --host 127.0.0.1 --port 8080
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable robocall

echo
echo "Далі: 1) впишіть SIP-дані Binotel у /etc/asterisk/pjsip.conf і зробіть 'asterisk -rx \"pjsip reload\"'"
echo "      2) заповніть /opt/robocall/.env"
echo "      3) systemctl start robocall"
echo "      4) поставте nginx/caddy з HTTPS перед 127.0.0.1:8080 для вебхука"
