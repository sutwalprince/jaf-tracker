#!/usr/bin/env bash
# Runs on the server. Puts Nginx in front of the website and opens web ports in the server's own firewall.
# Oracle's security list must also allow ports 80 and 443 before anyone can connect.
set -euo pipefail

DOMAIN=$(python3 -c 'import json, pathlib; print(json.loads((pathlib.Path.home() / "jaf-sync/site.json").read_text())["domain"])' 2>/dev/null || true)
if [ -z "$DOMAIN" ]; then
  echo 'Refusing to continue: set your domain first, e.g.  echo '"'"'{"domain": "jaf.example.com", "user": "me"}'"'"' > ~/jaf-sync/site.json' >&2
  exit 1
fi
if [ ! -f "$HOME/jaf-sync/web-password" ]; then
  echo "Refusing to continue: the website has no password yet." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
sudo -E apt-get -o DPkg::Lock::Timeout=180 update -y -qq
sudo -E apt-get -o DPkg::Lock::Timeout=180 install -y -qq nginx certbot python3-certbot-nginx >/dev/null

sudo tee /etc/nginx/snippets/jaf-proxy.conf >/dev/null <<'EOF'
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header Connection "";
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
EOF

# HTTPS settings are added to this file by Certbot, so only install it the first time.
if [ ! -f /etc/nginx/sites-available/jaf-tracker ]; then
  sed "s/jaf\.example\.com/$DOMAIN/g" "$HOME/public-site.nginx.conf" | sudo tee /etc/nginx/sites-available/jaf-tracker >/dev/null
  sudo ln -sf /etc/nginx/sites-available/jaf-tracker /etc/nginx/sites-enabled/jaf-tracker
  sudo rm -f /etc/nginx/sites-enabled/default
fi
rm -f "$HOME/public-site.nginx.conf"
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

# Live rules, then the saved rules directly. Tailscale manages its own chains, so a
# full "save" would write those into the boot file as well.
for port in 443 80; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  fi
  if [ -f /etc/iptables/rules.v4 ] && ! sudo grep -qE -- "^-A INPUT .*--dport $port " /etc/iptables/rules.v4; then
    sudo sed -i "0,/^-A INPUT .*--dport 22 .*/s//-A INPUT -p tcp -m state --state NEW -m tcp --dport $port -j ACCEPT\n&/" /etc/iptables/rules.v4
  fi
done

echo "Public site is set up on this server."
