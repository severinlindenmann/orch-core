# Pilot server runbook

For the person who sets up the one shared server (a VPS) for the pilot. Plain steps, no coding. Words in `code` are typed
exactly. "Your private network (VPN)" is the network your Macs and the server share; the server has an address in it that
starts with `100.` (the range 100.64.0.0 to 100.127.255.255).

The server has three parts and two OS users:

| User | Parts | Reached as |
|---|---|---|
| `orch-edge` | relay (phone push), publish (shares) | `https://relay.<domain>`, `https://publish.<domain>` (open to the internet) |
| `orch-hub` | hub | `https://hub.<domain>` (only from your private network) and the hub link on the private-network address, port 8105 |

Each user runs only its own parts, as systemd **user** services. Nothing runs as root except the few printed steps below.

## 1. DNS records (at your domain provider)

Add an **A** record (and an **AAAA** record if the server has an IPv6 address) for each name, all pointing at the server's
public address:

```
hub.example.com      A     <server public IPv4>
relay.example.com    A     <server public IPv4>
publish.example.com  A     <server public IPv4>
```

The hub name must also exist publicly, even though the hub only answers on your private network: the certificate is
requested from the internet (the `HTTP-01` check on port 80). Caddy answers that check before it looks at who is asking, so
the "private network only" rule never blocks it.

Wait a few minutes. `orch` checks the records for you and says so in plain words if one is missing or points somewhere else.

## 2. The hub name inside your private network

On your private network a Mac must reach `hub.example.com` through the **private** address of the server, not the public
one (the hub refuses everything that does not come from the private network, and a request that goes out to the public
address looks like it comes from the internet). Choose one:

1. **A private-network DNS record (recommended).** In the admin page of your private network, add a DNS record:
   `hub.example.com` -> the server's private address (`100.x.y.z`). Do it once; every Mac and the server pick it up.
2. **One line per Mac.** On each Mac, add to `/etc/hosts` (needs your Mac password):
   `100.x.y.z  hub.example.com`

Test on a Mac: `curl -sI https://hub.example.com` should answer (not hang, not 403).

## 3. The two users and the root steps

You need an administrator login on the server once. Create the two users (no password login needed):

```
sudo adduser --disabled-password --gecos "" orch-edge
sudo adduser --disabled-password --gecos "" orch-hub
```

Also recommended on a small server (this one has 1.9 GB of memory and no swap): add a 2 GB swap file.

```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

The setup command prints the other root steps as **one block**; `orch` never runs it. It makes the group `orch-caddy`, a
folder `/etc/caddy/orch.d` that group may write, one `import` line in the Caddyfile, a Caddy reload, and "linger" (so a
user's services keep running after logout). Run that block once (you can run it again after a change; it is safe to repeat).
Anyone in the group `orch-caddy` can change what Caddy serves: keep that group to `orch-edge` and `orch-hub`.

## 4. Install orch

For each user, log in as that user (`sudo -iu orch-edge`) and run the installer with `--server`:

```
curl -fsSL <installer address> | sh -s -- --server
```

(The installer address is the one in the release notes.) This installs `orch` for that user only.

## 5. Set up each role

As **`orch-edge`**:

```
orch setup --server --role edge --domain example.com
```

As **`orch-hub`**:

```
orch setup --server --role hub --domain example.com --hub-vpn-only
```

What happens, in order: the names are derived (`hub.`, `relay.`, `publish.` + your domain; override one with
`--domain-hub`, `--domain-relay`, `--domain-publish`); the DNS check runs; the parts start on the server's own address
only (127.0.0.1); one Caddy file per name is written into `/etc/caddy/orch.d/`; the root block is printed; a health timer
is installed (and, for the hub, the nightly backup copy).

- If a name does not resolve yet, that part's secure address is skipped, and the command tells you to **run it again** after
  fixing DNS. Running it again is safe.
- If `/etc/caddy/orch.d` does not exist yet, run the root block first and run the command again.
- The hub link binds the server's private-network address (found by itself; give it with `--vpn-ip 100.x.y.z` if the server
  has several). The hub's card advertises that address and port 8105, plus `https://hub.example.com` for the console.
- Without `--domain` there is no Caddy and no https: the parts listen on an IP address and a port and the command prints
  them (for a trial on a local network).
- `orch setup --server ...` and `orch install server ...` are the same command.

After the root block has run (and Caddy reloaded), open `https://hub.example.com` from a Mac on the private network. The
first-admin link is printed by the hub setup.

## 6. Connect the Macs

- Make sure a Mac resolves `hub.example.com` to the private address (step 2).
- Publish: on each Mac, once: `orch publish setup --server https://publish.example.com --share https://publish.example.com`
  (the setup command prints this line). Server and share are the same address.
- The Mac's workspace joins the hub from the hub console (it dials the private address and port 8105 the hub card names).
- Phones use the relay at `https://relay.example.com`.

## 7. Update order

Always the **server first, then the Macs**: update `orch-edge`, then `orch-hub` (`orch update` as each user), then each Mac.
A Mac that is newer than the server may be refused; the other way round works.

## 8. Backups and restore

- The hub makes its own sealed backup every day, once you set the backup key in the hub console (write the 24 words down on
  paper; without them a backup cannot be opened).
- At 03:40 a timer copies the sealed files into `/home/orch-hub/orch-backup-out/` (the newest 14). The files are sealed:
  the folder holds nothing a thief can read. **The server holds no password or key of your Mac.**
- The Mac pulls them (this runs on the Mac, e.g. from a nightly job), over the private network:

  ```
  rsync -a --ignore-existing orch-hub@100.x.y.z:orch-backup-out/ ~/orch-backups/
  ```

  This needs your Mac's public SSH key in `orch-hub`'s `authorized_keys` and a login shell for `orch-hub` (an administrator
  adds both, once).
- Restore on a fresh server: `orch-hub restore <file.orchbak>` and type the 24 words (see `orch-hub restore --help`).

## 9. When something fails

- `systemctl --user status orch-health.timer` and `journalctl --user -u orch-health.service -n 50` as the same user show the
  health warnings: a part not answering, less than 2 GB disk, the clock not synchronised, a certificate expiring within 14
  days, less than 200 MB of free memory. The latest result is also in `~/.local/state/orch/install/ops/health-status.json`
  (or under your orch state folder).
- `orch install status` shows each part. `journalctl --user -n 100` shows the services.
- "No DNS record" or "points at ...": fix the record, wait, run the setup command again.
- Browser says 403 on the hub: you are not on the private network, or the Mac resolves the name to the public address
  (step 2).
- Certificate error: check ports 80 and 443 are open at the server's firewall and that the DNS record is the public address.
  Look at Caddy: `sudo journalctl -u caddy -n 100`.
- Out of memory: add the swap file (step 3).

## What you decide

1. The private-network DNS record (option 1) or the `/etc/hosts` lines (option 2).
2. The domain name.
3. Whether the old development Caddy sites on the server may be removed.
