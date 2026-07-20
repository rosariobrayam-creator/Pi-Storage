# Pi Photo Server

A small Flask app for your Raspberry Pi. Your iPhone sends photos to it over HTTP (via the Shortcuts app or Safari); the Pi validates them, stores them in date folders (`photos/2026/07/10/...`), skips duplicates, records metadata in SQLite, and serves a web gallery.

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole server |
| `requirements.txt` | Python dependencies |
| `config.env.example` | Settings template (API key, port, limits) |
| `photo-server.service` | systemd unit so it runs on boot |

## 1. Install on the Pi

```bash
sudo apt update && sudo apt install -y python3-venv libheif1
mkdir -p ~/photo-server && cd ~/photo-server
# copy app.py, requirements.txt, config.env.example, photo-server.service here
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

If `pillow-heif` fails to build (can happen on 32-bit Pi OS), try `sudo apt install libheif-dev` and retry — or remove it from requirements.txt and add a "Convert Image → JPEG" step to your Shortcut instead.

## 2. Configure

```bash
cp config.env.example config.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # generate a key
nano config.env                                                  # paste it as API_KEY=...
```

## 3. Run and test

```bash
cd ~/photo-server
set -a; source config.env; set +a
venv/bin/python app.py
```

Find the Pi's IP with `hostname -I`. From a laptop on the same Wi-Fi:

```bash
curl http://PI_IP:8000/health
curl -X POST http://PI_IP:8000/upload -H "X-API-Key: YOURKEY" -F file=@test.jpg
```

You should get `{"stored": 1, ...}`; sending the same file again returns `"duplicate"`. Photos land in `photos/YYYY/MM/DD/`, logs in `logs/server.log`.

## 4. Start on boot

```bash
sudo cp photo-server.service /etc/systemd/system/
# edit it first if your user/paths aren't /home/pi/photo-server
sudo systemctl daemon-reload
sudo systemctl enable --now photo-server
journalctl -u photo-server -f    # watch logs
```

Give the Pi a fixed address (DHCP reservation in your router) so the Shortcut URL never breaks.

## 5. iPhone Shortcut (manual + Share Sheet)

1. Shortcuts app → **+** → name it "Send to Pi".
2. Add **Select Photos**; enable *Select Multiple*.
3. Add **Repeat with Each** (input: the selected Photos).
4. Inside the repeat, add **Get Contents of URL**:
   - URL: `http://PI_IP:8000/upload`
   - Method: **POST**
   - Headers: `X-API-Key` = your key
   - Request Body: **Form** → add field: type **File**, key `file`, value **Repeat Item**
   - Optional second form field: type Text, key `device`, value `iphone`
5. Optionally add **Show Result** after the repeat.
6. In the shortcut's settings (ⓘ), enable **Show in Share Sheet** → accepts **Images**. Now you can share any photo(s) from the Photos app straight to the Pi.

No Shortcut needed at all if you prefer Safari: open `http://PI_IP:8000/upload?key=YOURKEY` and pick photos there.

## 6. Automatic upload

Shortcuts has no "when a photo is taken" trigger, but this works well:

1. Shortcuts → **Automation** → **+** → **App** → choose **Camera** → *Is Closed* → **Run Immediately**.
2. Have it run a shortcut that does **Find Photos** (filter: *Date Taken is in the last 1 hour*, limit ~20) → Repeat with Each → the same **Get Contents of URL** POST as above.

Re-sending overlapping photos is harmless — the server detects duplicates by content hash and skips them.

## 7. Gallery

Open `http://PI_IP:8000/?key=YOURKEY` — thumbnails newest-first, tap for the original.

## 8. Remote access and HTTPS

- **Same Wi-Fi only:** works as-is.
- **Away from home (recommended):** install [Tailscale](https://tailscale.com) on the Pi and iPhone. Use the Pi's Tailscale IP/name in the Shortcut. Traffic is WireGuard-encrypted end to end and no router ports are opened.
- **HTTPS:** on a plain LAN the API key travels unencrypted. To fix, put Caddy in front:

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

  Then use `https://PI_IP:8443` (self-signed; or use a real domain for Let's Encrypt, or `tailscale cert` on a tailnet).

## Troubleshooting

- **401** — key mismatch between Shortcut and `config.env`.
- **413** — request too big; raise `MAX_UPLOAD_MB`.
- **507** — Pi disk nearly full (below `MIN_FREE_MB`).
- **HEIC rejected** — `pillow-heif` isn't installed; see step 1, or convert to JPEG in the Shortcut.
- **Backups** — SD cards fail. Sync the photo folder off the Pi occasionally, e.g. `rsync -a ~/photo-server/photos/ user@nas:/backup/photos/` (cron it).
