# Pi-Storage

A small Flask app for your Raspberry Pi. Your iPhone sends photos to it over HTTP (via the Shortcuts app or Safari); the Pi validates them, stores them in date folders (`photos/u1/2026/07/10/...`), skips duplicates, records metadata in SQLite, and serves a web gallery.

**Multi-user.** Each account has its own library and can only see its own photos. The browser signs in with a username and password; the iPhone Shortcut authenticates with a per-account upload token, so uploads land in the right library.

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole server |
| `templates/` | Page templates (Jinja) — includes the Pi-Storage logo, `_logo.svg` |
| `static/` | `app.css`, `app.js`, `icon.svg` — all self-hosted, works offline |
| `requirements.txt` | Python dependencies |
| `config.env.example` | Settings template |
| `photo-server.service` | systemd unit so it runs on boot |

## 1. Install on the Pi

```bash
sudo apt update && sudo apt install -y python3-venv libheif1
mkdir -p ~/photo-server && cd ~/photo-server
# copy app.py, templates/, static/, requirements.txt, config.env.example,
# photo-server.service here
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

`templates/` and `static/` must sit next to `app.py`. Deploying `app.py` alone will start the server but every page will 500.

If `pillow-heif` fails to build (can happen on 32-bit Pi OS), try `sudo apt install libheif-dev` and retry — or remove it from requirements.txt and add a "Convert Image → JPEG" step to your Shortcut instead.

## 2. Configure

```bash
cp config.env.example config.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # for INVITE_CODE
nano config.env
```

Nothing is strictly required. The two settings worth knowing:

- **`INVITE_CODE`** — set it to open the `/signup` page. Anyone with the code can create an account. Leave it unset and signup returns 503; create accounts on the Pi instead (step 3).
- **`SECRET_KEY`** — signs session cookies. If you leave it unset, one is generated on first run and saved to `secret.key`. Either way it must survive restarts, or every restart signs everyone out. Don't delete `secret.key`.

If you ran an earlier single-key version of this server, leave `API_KEY` and `ALLOW_LEGACY_KEY=1` in place for now — see [Upgrading](#upgrading-from-the-single-key-version).

## 3. Create your account

```bash
cd ~/photo-server
set -a; source config.env; set +a
venv/bin/python app.py create-user brayam
```

It prompts for a password and prints an upload token. **Copy the token** — it's stored hashed, so it can't be shown again (you can always issue a new one from the account page, or with `venv/bin/python app.py rotate-token brayam`).

The first account created becomes the admin and inherits any photos that were uploaded before accounts existed.

## 4. Run and test

```bash
venv/bin/python app.py
```

Find the Pi's IP with `hostname -I` — it prints IPv4 first, then IPv6. You want the IPv4 one (like `192.168.1.214`).

> **Placeholders.** Everywhere below, `<PI_IP>` means your Pi's actual IPv4 address and `<TOKEN>` means the upload token from step 3. Substitute both — pasting them literally fails with "could not resolve host" or a 401.

From a laptop on the same Wi-Fi:

```bash
curl http://<PI_IP>:8000/health
curl -X POST http://<PI_IP>:8000/upload -H "X-API-Key: <TOKEN>" -F "file=@test.jpg"
```

On **Windows PowerShell**, use `curl.exe` — plain `curl` is an alias for `Invoke-WebRequest`, which doesn't understand `-X`/`-H`/`-F` and fails with "Cannot bind parameter 'Headers'":

```powershell
curl.exe http://<PI_IP>:8000/health
curl.exe -X POST http://<PI_IP>:8000/upload -H "X-API-Key: <TOKEN>" -F "file=@C:/path/to/test.jpg"
```

Note the `@` before the path — without it curl sends the path as text instead of the file. Quote the whole `file=@...` argument so paths containing spaces survive.

You should get `{"stored": 1, "owner": "brayam", ...}`; sending the same file again returns `"duplicate"`. Photos land in `photos/u<your-id>/YYYY/MM/DD/`, logs in `logs/server.log`.

## 5. Start on boot

```bash
sudo cp photo-server.service /etc/systemd/system/
# edit it first if your user/paths aren't /home/prdx/photo-server
sudo systemctl daemon-reload
sudo systemctl enable --now photo-server
journalctl -u photo-server -f    # watch logs
```

Give the Pi a fixed address (DHCP reservation in your router) so the Shortcut URL never breaks.

Redeploying after a change — templates and static files are read from disk at startup, so a restart is required:

```bash
rsync -av app.py requirements.txt prdx@<PI_IP>:/home/prdx/photo-server/
rsync -av --delete templates/ prdx@<PI_IP>:/home/prdx/photo-server/templates/
rsync -av --delete static/    prdx@<PI_IP>:/home/prdx/photo-server/static/
ssh prdx@<PI_IP> 'sudo systemctl restart photo-server'
```

## 6. iPhone Shortcut (manual + Share Sheet)

1. Shortcuts app → **+** → name it "Send to Pi".
2. Add **Select Photos**; enable *Select Multiple*.
3. Add **Repeat with Each** (input: the selected Photos).
4. Inside the repeat, add **Get Contents of URL**:
   - URL: `http://<PI_IP>:8000/upload` — type your Pi's real IP here, e.g. `http://192.168.1.214:8000/upload`. Pasting `<PI_IP>` literally gives "a server with the specified hostname could not be found."
   - Method: **POST**
   - Headers: `X-API-Key` = your upload token from step 3
   - Request Body: **Form** → add field: type **File**, key `file`, value **Repeat Item**
   - Optional second form field: type Text, key `device`, value `iphone`
5. Optionally add **Show Result** after the repeat.
6. In the shortcut's settings (ⓘ), enable **Show in Share Sheet** → accepts **Images**. Now you can share any photo(s) from the Photos app straight to the Pi.

No Shortcut needed at all if you prefer Safari: open `http://<PI_IP>:8000/upload`, sign in, and pick photos there.

## 7. Automatic upload

Shortcuts has no "when a photo is taken" trigger, but this works well:

1. Shortcuts → **Automation** → **+** → **App** → choose **Camera** → *Is Closed* → **Run Immediately**.
2. Have it run a shortcut that does **Find Photos** (filter: *Date Taken is in the last 1 hour*, limit ~20) → Repeat with Each → the same **Get Contents of URL** POST as above.

Re-sending overlapping photos is harmless — the server detects duplicates by content hash and skips them.

## 8. Gallery

Open `http://<PI_IP>:8000/` and sign in. Newest first, click any photo for the full-size view (arrow keys page through it, Escape closes).

HEIC photos display fine in any browser: the Pi transcodes a JPEG copy on the fly for viewing, while **Download original** in the lightbox still gives you the untouched `.heic` file.

## 9. Adding other people

Two options:

- Set `INVITE_CODE` in `config.env`, restart, and send them the code plus `http://<PI_IP>:8000/signup`.
- Or run `venv/bin/python app.py create-user <name>` on the Pi and hand over the password and token yourself.

Either way their photos are theirs alone — the gallery, thumbnails, full-size views and downloads are all scoped to the signed-in account, and someone else's photo ID returns a 404.

## 10. Remote access and HTTPS

- **Same Wi-Fi only:** works as-is.
- **Away from home (recommended):** install [Tailscale](https://tailscale.com) on the Pi and iPhone. Use the Pi's Tailscale IP/name in the Shortcut. Traffic is WireGuard-encrypted end to end and no router ports are opened.
- **HTTPS:** on a plain LAN, passwords and tokens travel unencrypted. To fix, put Caddy in front:

  ```bash
  sudo apt install caddy
  ```

  `/etc/caddy/Caddyfile`:

  ```
  :8443 {
      tls internal
      reverse_proxy 127.0.0.1:8000
  }
  ```

  Then use `https://<PI_IP>:8443` (self-signed; or use a real domain for Let's Encrypt, or `tailscale cert` on a tailnet). Once you're on HTTPS, set `SESSION_COOKIE_SECURE=1` and `TRUST_PROXY=1` in `config.env`.

  Don't set `SESSION_COOKIE_SECURE=1` while still on plain `http://` — the browser silently refuses to store the cookie, and login will appear to work but never stick.

## Upgrading from the single-key version

The database migrates itself on first start; it makes a `photos.db.bak-v0` backup first.

1. Deploy the new `app.py`, `templates/` and `static/`, then restart. Existing photos are left unowned for now, and the log says so.
2. Create your account (step 3). It becomes the admin and claims all the existing photos.
3. Your old Shortcut keeps working the whole time — while `ALLOW_LEGACY_KEY=1`, the old shared `API_KEY` still uploads, attributed to the admin account. Each use logs a warning.
4. When you're ready, change the Shortcut's `X-API-Key` header to your personal token, then set `ALLOW_LEGACY_KEY=0` in `config.env` and restart.

## Troubleshooting

- **Redirected to the login page over and over** — the session cookie isn't sticking. Almost always `SESSION_COOKIE_SECURE=1` while browsing over plain `http://`. Unset it.
- **Everyone logged out after a restart** — `secret.key` was deleted or `SECRET_KEY` changed. Both must be stable.
- **Broken image icons in the gallery** — check `logs/server.log`. A thumbnail the Pi can't decode serves a placeholder with an `X-Thumb-Error` header rather than failing the page. Also make sure you're browsing the live `http://<PI_IP>:8000/` URL, not a saved copy of the page — a saved `.html` resolves its image URLs against your own PC and shows nothing.
- **401** — the token doesn't match. Issue a new one from the account page.
- **413** — request too big; raise `MAX_UPLOAD_MB`.
- **507** — Pi disk nearly full (below `MIN_FREE_MB`).
- **HEIC rejected on upload** — `pillow-heif` isn't installed; see step 1, or convert to JPEG in the Shortcut. Sign in and open `/health` to see whether the running service has HEIC support.
- **Signup says 503** — `INVITE_CODE` isn't set in `config.env`.
- **Backups** — SD cards fail. Sync the photo folder and the database off the Pi occasionally, e.g. `rsync -a ~/photo-server/photos/ ~/photo-server/photos.db user@nas:/backup/` (cron it).
