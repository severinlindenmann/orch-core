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

The "private network only" rule lets in the real network of your VPN, read from the server's VPN interface (for example
`100.92.0.0/16`), not the whole 100.64.0.0/10 range. If the server cannot tell, give it with `--vpn-ip 100.92.1.2/16`. The rule
relies on Caddy being the **first hop from the internet**: do not put a proxy or load balancer in front of it, because
Caddy would then see the proxy's address instead of the visitor's.

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

The setup command prints the other root steps as **one block**; `orch` never runs it. For the user it is run for it
makes `/etc/caddy/orch.d/<user>/` (owned by that user, mode 0755; there is no shared writable folder), adds one `import
/etc/caddy/orch.d/<user>/*.caddy` line for that user to the Caddyfile (Caddy allows only one `*` per pattern, so each
server user has its own line: `/etc/caddy/orch.d/orch-hub/*.caddy` and `/etc/caddy/orch.d/orch-edge/*.caddy`), reloads Caddy, and turns on "linger" (so the user's services keep
running after logout). Run the block once per user (it is safe to repeat; the `import` line is added only once).

**Residual risk, in plain words:** Caddy serves whatever the snippets of either user say. A person who controls the
`orch-edge` account can therefore write a snippet for a new host name, including one that answers instead of a name the other
user owns. "Hub only from the private network" is a rule inside the hub's own snippet; it is **not** a wall against a
compromised `orch-edge` user. That is why the two users exist: keep the edge user (the one facing the internet) free of
anything you would not want an attacker to have, and give it nothing from the hub.

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
- `--port-base N` moves all ports in one block: hub N, relay N+1, publish N+2, hub link N+5 (default N = 8100). A port that
  another program already holds is refused before anything is installed; nothing is stopped for you.
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
- At 03:40 a timer (running as `orch-hub`) copies the sealed files (the newest 14) into a **drop folder** that belongs to a
  separate user, `orch-backup`. The files are sealed: they hold nothing a thief can read. **The server holds no password or
  key of your Mac**, and `orch-edge` cannot read the folder.
- One-time root steps (an administrator; `orch-hub` needs no login shell and `orch-backup` gets no password):

  ```
  sudo adduser --disabled-password --gecos "" orch-backup
  sudo chmod 700 /home/orch-backup
  sudo install -d -m 3770 -o orch-backup -g orch-backup /var/lib/orch-backup-drop
  sudo usermod -aG orch-backup orch-hub
  sudo systemctl restart user@$(id -u orch-hub).service
  ```

  The folder is owned by `orch-backup`, its group `orch-backup` may write (so `orch-hub`, a member, can add its nightly
  files; the sticky bit means members can only remove or rename their own files, so no one can swap the hub's), and
  everyone else (including `orch-edge`) has no access. The path must be a full path whose parent folders belong to root
  and are not writable by anyone else (setup refuses otherwise). The last line restarts `orch-hub`'s services so
  the new group membership applies. Then run the hub setup with the drop folder:
  `orch setup --server --role hub ... --backup-drop /var/lib/orch-backup-drop`.
- The Mac pulls them over the private network, from the Mac (for example a nightly job):

  ```
  rsync -a --ignore-existing --exclude '.*' orch-backup@100.x.y.z: ~/orch-backups/
  ```

  The path after the colon is empty because the server limits `orch-backup` to that one folder; `--exclude '.*'` keeps an
  unfinished (hidden `.part`) file from ever being pulled. In `/home/orch-backup/.ssh/authorized_keys`
  put your Mac's public key restricted to a single read-only command and to the private network, all on one line:

  ```
  restrict,from="100.64.0.0/10",command="rrsync -ro /var/lib/orch-backup-drop" ssh-ed25519 AAAA... mac-backup
  ```

  (`restrict` switches off shells, port forwarding and agent forwarding; `rrsync -ro` allows only reading that folder. Use your
  VPN's real network instead of `100.64.0.0/10` if you know it, for example `100.92.0.0/16`.) **Do not give `orch-hub` a login
  shell or put your Mac's key on it.**
- `rrsync` comes with the `rsync` package on current Ubuntu (`/usr/bin/rrsync`; check with `command -v rrsync`). If it is
  missing, install the package (`sudo apt install rsync`); on an older package the script is `/usr/share/doc/rsync/scripts/rrsync`
  (`.gz`: `gunzip -c ... | sudo tee /usr/local/bin/rrsync >/dev/null && sudo chmod 755 /usr/local/bin/rrsync`). Use the full path
  in `command=` if it is not in the default path.
- Restore, for example on a fresh server: (1) as the hub user, run the hub setup first (`orch setup --server --role hub ...`),
  so the user, the state folder and the services exist; (2) stop the hub:
  `systemctl --user stop "orch-*-hub.service"` (list the exact name with `systemctl --user list-units 'orch-*'`); (3) copy the
  chosen `.orchbak` file to the server; (4) run
  `orch-hub restore --state ~/.config/orch/install/parts/hub/data <file.orchbak>` (the state folder is the `data` folder shown by
  `orch install status`) and type the 24 words; (5) start the hub again with `systemctl --user start "orch-*-hub.service"`.

## 9. When something fails

- `systemctl --user status orch-health.timer` and `journalctl --user -u orch-health.service -n 50` as the same user show the
  health warnings: a part not answering, less than 2 GB disk, the clock not synchronised, a certificate expiring within 14
  days, less than 200 MB of free memory. The latest result is also in `install/ops/health-status.json` inside your orch state folder.
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
