# Run these as an administrator (they are the only steps that need root; orch never runs them):
sudo install -d -m 0755 -o root -g root /etc/caddy/orch.d
sudo install -d -m 0755 -o orch-edge -g orch-edge /etc/caddy/orch.d/orch-edge
grep -qxF 'import /etc/caddy/orch.d/*/*.caddy' /etc/caddy/Caddyfile || echo 'import /etc/caddy/orch.d/*/*.caddy' | sudo tee -a /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy
sudo loginctl enable-linger orch-edge
