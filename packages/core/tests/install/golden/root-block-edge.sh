# Administrator steps for orch-edge (the only steps that need root; orch never runs them).
# The other server user gets its own block and its own import line when it is set up.
sudo install -d -m 0755 -o root -g root /etc/caddy/orch.d
sudo install -d -m 0755 -o orch-edge -g orch-edge /etc/caddy/orch.d/orch-edge
grep -qxF 'import /etc/caddy/orch.d/orch-edge/*.caddy' /etc/caddy/Caddyfile || echo 'import /etc/caddy/orch.d/orch-edge/*.caddy' | sudo tee -a /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy
sudo loginctl enable-linger orch-edge
