# fluent-monitor in Docker, behind the eduVPN tunnel (Unraid)

Two containers:

- **wireguard** (linuxserver/wireguard): connects to eduVPN with your WireGuard file.
- **fluent-monitor**: the same monitor you run on your laptop (about 110 MB). It uses the wireguard
  container's network, so everything it does goes through the tunnel. It reads the report
  files straight from the university's SMB share (Windows file share). No mounting is
  needed on Unraid and no extra privileges in the container.

Only these two containers use the tunnel; Unraid itself and your other containers do not.
The web page is published on the Unraid server's port 8050, reachable from your LAN, and
from your phone over your own Tailscale if Unraid is on it.

```
phone ──LAN / your tailnet──▶ Unraid:8050 ──▶ [wireguard ◀─ fluent-monitor] ══eduVPN══▶ \\server\share
```

## 1. Copy the project to Unraid

Copy the whole `fluent-monitor` folder (the one containing `Dockerfile`), e.g. to
`/mnt/user/appdata/fluent-monitor`. All commands below run in its `docker` subfolder:

```bash
cd /mnt/user/appdata/fluent-monitor/docker
```

You need `docker compose`: install the **Docker Compose Manager** plugin (Apps tab) if
`docker compose version` doesn't work.

## 2. WireGuard file

Put the file from the eduVPN portal here, named exactly `wg0.conf`:

```
docker/wireguard/wg_confs/wg0.conf
```

Then look at its `AllowedIPs` line:

- **Only university ranges** (no `0.0.0.0/0`): nothing to do.
- **`0.0.0.0/0`** (everything through the VPN): replies to your phone would also go into
  the tunnel and never arrive. Add these two lines to the `[Interface]` section so that
  answers to your home network and your tailnet leave the normal way. Adjust
  `192.168.0.0/16` to your LAN, e.g. `192.168.1.0/24`, if the university also uses
  192.168.x.x addresses.

  ```ini
  PostUp = GW=$(ip route show default | awk '{print $3; exit}'); ip route add 192.168.0.0/16 via $GW; ip route add 100.64.0.0/10 via $GW
  PreDown = ip route del 192.168.0.0/16; ip route del 100.64.0.0/10
  ```

The file contains your private key: `chmod 600 wireguard/wg_confs/wg0.conf`.

## 3. Settings

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

- `FLUENT_FOLDER`: the case folder to start with, as an SMB path: `smb://server/share/sub/folder`.
  On Windows, run `net use` in cmd to see which `\\server\share` is behind your drive
  letter. `Z:\Semesterarbeit\Fluent` on `\\fs01.uni.example\home$\pedro` becomes
  `smb://fs01.uni.example/home$/pedro/Semesterarbeit/Fluent`.
  If the name doesn't resolve over the VPN, use the server's IP address instead.
- `FM_ROOT` (optional): the web page can switch to any folder below this one. Empty means
  the whole share; set it to e.g. `smb://fs01.uni.example/home$/pedro/Semesterarbeit` to
  show only your own folders.
- `SMB_USERNAME`, `SMB_PASSWORD`, `SMB_DOMAIN`: the account you use for that share,
  normally your university login. The files are only read, never changed.
- `FM_PUBLIC_URL`: how you open Unraid from the phone, e.g. `http://tower:8050` or its
  tailnet name/IP. It's only used to print the right link.
- `FM_PORT`, `FM_INTERVAL`, `TZ`: port on Unraid, seconds between reads, time zone.

## 4. Build and start

```bash
docker compose up -d --build
docker compose logs fluent-monitor
```

The log shows the link with the access token. Open it once on the phone and bookmark it.
The token and your combined plots are kept in `docker/data`.

Check the tunnel with `docker compose logs wireguard`. It should end without errors, and
`docker exec fluent-wireguard wg show` should list a recent *latest handshake*.

## Day to day

- **eduVPN file expired** (90 days by default): download a new one, replace `wg0.conf`, then
  `docker compose restart wireguard`. The monitor restarts with it. Until then, the page
  shows "report folder not reachable (VPN down?)" and keeps the data it already has.
- **After changing the code:** `docker compose up -d --build`.
- **Stop:** `docker compose down`.
- **Another case folder:** in the web page, open ☰ → *Change folder…* (or click the folder
  name at the top), browse to the case and press *Watch this folder*. Every open browser
  switches along. The choice survives restarts. The folder dialog also lists recently
  used folders, and selections and combined plots are remembered per folder.
  Editing `FLUENT_FOLDER` in `.env` still works: a changed value wins once at the next
  `docker compose up -d`.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| page doesn't load from the phone, but `curl localhost:8050` on Unraid answers | `AllowedIPs = 0.0.0.0/0` without the `PostUp` lines from step 2 |
| "report folder not reachable" | the tunnel is down (`docker compose logs wireguard`), the share path is wrong, or the password is wrong (`docker compose logs fluent-monitor` shows the SMB error) |
| `STATUS_LOGON_FAILURE` | wrong username or password; try `SMB_USERNAME=user@uni.example` or set `SMB_DOMAIN` |
| `STATUS_BAD_NETWORK_NAME` | the share name (first part after the server) is wrong; check `net use` on Windows |
| name doesn't resolve | use the server's IP address in `FLUENT_FOLDER` |
