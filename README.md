# Pi-Storage

A small Flask app for your Raspberry Pi. Your iPhone sends photos to it over HTTP (via the Shortcuts app or Safari); the Pi validates them, stores them in date folders (`photos/u1/2026/07/10/...`), skips duplicates, records metadata in SQLite, and serves a web gallery.

**Multi-user.** Each account has its own library and can only see its own photos. The browser signs in with a username and password; the iPhone Shortcut authenticates with a per-account upload token, so uploads land in the right library.

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole server |
| `templates/` | Page templates (Jinja) — includes the Pi-Storage logo, `_logo.svg` |
| `static/` | `app.css`, `app.js`, app icons — all self-hosted, works offline |
| `tools/make_icons.py` | Regenerates the logo SVGs and PNG app icons |
| `requirements.txt` | Python dependencies |
| `config.env.example` | Settings template |
| `pi-storage.service` | systemd unit so it runs on boot |

## 1. Install on the Pi

```bash
sudo apt update && sudo apt install -y python3-venv libheif1 ffmpeg
mkdir -p ~/Documents/Projects && cd ~/Documents/Projects
git clone <your-repo-url> Pi-Storage && cd Pi-Storage
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

`templates/` and `static/` must sit next to `app.py`. Deploying `app.py` alone will start the server but every page will 500.

If `pillow-heif` fails to build (can happen on 32-bit Pi OS), try `sudo apt install libheif-dev` and retry — or remove it from requirements.txt and add a "Convert Image → JPEG" step to your Shortcut instead.

`ffmpeg` is what makes video thumbnails. Without it videos still upload and play; their tiles just show a placeholder instead of a poster frame.

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
cd ~/Documents/Projects/Pi-Storage
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
sudo cp pi-storage.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-storage
journalctl -u pi-storage -f    # watch logs
```

The three paths in `pi-storage.service` must match wherever `app.py` actually
lives. Run `pwd` in that folder and use exactly what it prints. If they're
wrong, systemd fails with the unhelpful "unavailable resources or another
system error" and never mentions the path.

To repoint an already-installed unit at a different folder, edit the installed
copy — not the repo copy, which systemd never reads:

```bash
sudo sed -i 's|/old/path|/new/path|g' /etc/systemd/system/pi-storage.service
sudo systemctl daemon-reload && sudo systemctl restart pi-storage
```

Use `|` as the delimiter, not `/` — paths are full of slashes and `s/.../.../`
fails with ``unknown option to `s'``.

Stop any copy you started by hand first (`pkill -f app.py`) — two instances will fight over port 8000 and the database. `pgrep -af app.py` should show exactly one process afterwards.

Give the Pi a fixed address (DHCP reservation in your router) so the URL never breaks.

Redeploying after a change — templates and static files are read from disk at startup, so a restart is required:

```bash
cd ~/Documents/Projects/Pi-Storage && git pull && sudo systemctl restart pi-storage
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
6. In the shortcut's settings (ⓘ), enable **Show in Share Sheet** → accepts **Images** and **Media**. Now you can share any photo(s) or video(s) from the Photos app straight to the Pi.

Videos (.mov/.mp4) upload the same way as photos — no extra steps. Don't add a "Convert Image" action to the shortcut: it would strip video files and re-encode HEIC originals.

Once Tailscale is set up (§10), use the `https://<host>.<tailnet>.ts.net/upload` URL here instead of the LAN IP — then the Shortcut works from anywhere, not just at home.

No Shortcut needed at all if you prefer Safari: open `/upload`, sign in, and pick photos there.

### Clearing your phone (bulk offload)

The Repeat-with-Each loop sends **one file per request**, so there's no practical limit on how much one run can move — 40GB works. What makes it safe:

- **Interrupted runs resume for free.** The server recognises files it already has by content hash, so re-running the shortcut on the same selection skips everything already stored (each comes back `"duplicate"` in milliseconds) and continues where it left off.
- **Work in batches of 100–300 items.** Shortcuts itself gets flaky with thousands of selections; several smaller runs cost nothing extra thanks to dedup.
- **Same Wi-Fi as the Pi, phone plugged in.** Over Tailscale from outside, a big offload can crawl through a relay.

To also delete from the phone as you go, add inside the repeat, after Get Contents of URL:

1. **Get Dictionary from** → Contents of URL
2. **Get Dictionary Value** → key `stored` (and another for `duplicates`)
3. **If** stored + duplicates ≥ 1 → **Delete Photos** → Repeat Item

Deleted items sit in **Recently Deleted for 30 days** — that's the real safety net. Run the first batch *without* the delete step, eyeball the gallery, then enable it. A "disk full" response has no `stored` key, so the If-gate skips the delete and nothing is lost.

## 7. Automatic upload

Shortcuts has no "when a photo is taken" trigger, but this works well:

1. Shortcuts → **Automation** → **+** → **App** → choose **Camera** → *Is Closed* → **Run Immediately**.
2. Have it run a shortcut that does **Find Photos** (filter: *Date Taken is in the last 1 hour*, limit ~20) → Repeat with Each → the same **Get Contents of URL** POST as above.

Re-sending overlapping photos is harmless — the server detects duplicates by content hash and skips them.

## 8. Gallery

Open `http://<PI_IP>:8000/` and sign in. Newest first, click any photo for the full-size view (arrow keys page through it, Escape closes).

HEIC photos display fine in any browser: the Pi transcodes a JPEG copy on the fly for viewing, while **Download original** in the lightbox still gives you the untouched `.heic` file.

Videos show a poster frame with a duration badge and play right in the lightbox. One caveat: iPhones record HEVC by default, which plays fine in Safari (and most modern Chrome) but may refuse in older desktop browsers — the Pi serves the file as-is rather than melting itself trying to transcode video. **Download original** always works regardless.

**Capture details.** New uploads store the date taken, GPS location and camera model, read from the photo's EXIF (or the video's QuickTime tags). The lightbox shows the capture date and camera, and "📍 View on map" opens the spot in Maps. Screenshots and re-saved images legitimately have none — those fall back to the upload date. To fill in details for everything uploaded *before* this feature existed:

```bash
venv/bin/python app.py backfill-details   # safe to re-run; skips rows already filled
```

**Live Photos.** A Live Photo is really two files — the still plus a ~3s clip. When both land in the same account with matching filenames (`IMG_1234.HEIC` + `IMG_1234.MOV`), the server pairs them automatically, in either upload order: the grid shows one tile with a ◎ LIVE badge, and the lightbox's LIVE button plays the clip. The clip stops counting as a separate library item. Note the Shortcuts app sends only the still by default; the pairing kicks in when the clip arrives by any route (the browser upload page accepts both files at once, and `backfill-details` pairs halves that are already uploaded).

To pull photos back off the Pi — onto your camera roll or your PC — see [Getting photos back](#12-getting-photos-back).

## 9. Adding other people

Two options:

- Set `INVITE_CODE` in `config.env`, restart, and send them the code plus `http://<PI_IP>:8000/signup`.
- Or run `venv/bin/python app.py create-user <name>` on the Pi and hand over the password and token yourself.

Either way their photos are theirs alone — the gallery, thumbnails, full-size views and downloads are all scoped to the signed-in account, and someone else's photo ID returns a 404.

## 10. Access from anywhere (Tailscale)

On a plain LAN, passwords and tokens travel unencrypted, and the Pi is unreachable away from home. [Tailscale](https://tailscale.com) fixes both: a private WireGuard network between your own devices, with no router ports opened and no public exposure. It also issues a real HTTPS certificate, so there are no browser warnings.

**On the Pi:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed URL to authenticate, then install Tailscale on your iPhone and sign in with the same account.

**In the Tailscale admin console** (one-time): DNS → enable **MagicDNS**, then enable **HTTPS Certificates**. Both are required for the next command.

**Put HTTPS in front of the app:**

```bash
sudo tailscale serve --bg 8000
tailscale serve status
```

That gives you a URL like `https://raspberrypi.tailnet-name.ts.net`. Load it once from a browser before going further.

**Then harden the session cookie** in `config.env`:

```
SESSION_COOKIE_SECURE=1
TRUST_PROXY=1
```

and `sudo systemctl restart pi-storage`.

`TRUST_PROXY=1` isn't optional here. Tailscale Serve proxies from localhost, so without it every login looks like it came from `127.0.0.1` and the failed-login throttle would lock out everyone at once.

> **Once `SESSION_COOKIE_SECURE=1` is set, stop using `http://<PI_IP>:8000`.** The browser silently discards a Secure cookie over plain HTTP, so sign-in appears to succeed and then bounces you back to the login page. Use the `https://...ts.net` URL everywhere, **including at home** — Tailscale routes it over your LAN automatically, so there's no speed penalty. One URL everywhere is simpler anyway.

To undo, comment out `SESSION_COOKIE_SECURE=1` and restart.

## 11. Add it to your iPhone home screen

The gallery is a progressive web app, so it installs like a native one.

In **Safari** (this doesn't work in Chrome), open your `https://...ts.net` URL → Share → **Add to Home Screen**. You get the Pi-Storage aperture icon, and it launches fullscreen with no browser chrome.

Sign in once and iOS offers to save the password to Keychain, so afterwards it's one Face ID tap. The session lasts 30 days and survives restarts of both the phone and the Pi.

Optionally add a Shortcuts action too — Shortcuts → **+** → **Open URLs** → your URL. Name it "Pi Storage" and you can trigger it by voice.

## 12. Getting photos back

Three ways, depending on how many you want.

### One photo, straight to your camera roll

Open it in the gallery and **long-press the photo itself** → *Add to Photos*. That saves the **original** file at full resolution, not the downscaled view copy — the lightbox's "Save to Photos" button points at `/original/<id>?inline=1`, which serves the untouched bytes inline so iOS will offer to save them. iOS reads HEIC natively, so your iPhone photos round-trip back perfectly.

**Download original** does the same thing as a file download — on iPhone that lands in the **Files** app rather than Photos, which is usually not what you want. On a computer it's the right button.

### A batch of them

Tap **Select** at the top of the library, tap the photos you want (or **Select all**), then **Download zip**. You get the originals, untouched, with their real filenames. Capped at 500 photos per zip.

### Automatically, into your camera roll — "Get from Pi" Shortcut

This is the good one: it lands photos directly in Photos with no Files detour.

1. Shortcuts app → **+** → name it "Get from Pi".
2. **Get Contents of URL**
   - URL: `https://<PI_HOST>/api/photos?limit=50`
   - Method: **GET**
   - Headers: `X-API-Key` = your upload token
3. **Get Dictionary Value** — key `photos`, from the previous step's Contents.
4. **Repeat with Each** — input: that list.
5. Inside the repeat: **Get Dictionary Value** — key `original_url`, from **Repeat Item**.
6. Inside the repeat: **Get Contents of URL**
   - URL: the value from step 5
   - Method: **GET**
   - Headers: `X-API-Key` = your upload token *(needed again — each request authenticates on its own)*
7. Inside the repeat: **Save to Photo Album**.

Run it and the most recent 50 photos land in your camera roll.

**Make repeat runs incremental.** Each response includes `max_id`. Save that (a text file in iCloud Drive, or the Shortcuts *Set Variable* + a Data Jar style store) and pass it back next time as `?after_id=<max_id>` — you'll only download what's new instead of re-fetching everything. Photos already in your library aren't detected as duplicates on the way *down*, so without this you'll get repeats.

### The API, if you want to script it

Everything below is scoped to the account whose token you send, and works with either the session cookie or `X-API-Key`.

| Endpoint | What you get |
|---|---|
| `GET /api/photos` | JSON list, newest first. `?limit=` (max 1000), `?after_id=` for incremental pulls |
| `GET /original/<id>` | Untouched original, as a download |
| `GET /original/<id>?inline=1` | Untouched original, inline (long-press-savable on iOS) |
| `GET /photo/<id>` | Display copy — always browser-renderable, HEIC transcoded to JPEG |
| `GET /thumb/<id>` | 480px JPEG thumbnail |
| `GET /export.zip?ids=1,2,3` | Zip of those originals (max 500) |
| `GET /export.zip?all=1` | Zip of the whole library (max 500) |

```bash
curl -H "X-API-Key: <TOKEN>" "https://<PI_HOST>/api/photos?limit=5"
curl -H "X-API-Key: <TOKEN>" -o photos.zip "https://<PI_HOST>/export.zip?all=1"
```

A note on the caps: a token that leaks is bad, but a token that leaks *and* can pull your entire library in one request is worse. The 500-photo ceiling and the per-export log line in `logs/server.log` are there so a stolen token can't quietly drain everything in a single call.

## 13. Moving photos to a USB drive

The SD card fills up eventually. Because `stored_path` is recorded relative to `PHOTOS_DIR`, pointing it at a drive keeps every existing row working — as long as the files move with it.

**Format the drive as ext4.** exFAT and NTFS mount with permissions that commonly leave the drive unwritable by the service user while looking fine when you poke at it interactively — the same "works when I test it, broken in the service" trap as a mis-set `PHOTOS_DIR`.

```bash
lsblk                                   # find the device, e.g. /dev/sda1
sudo mkfs.ext4 -L pistorage /dev/sda1   # ERASES the drive
sudo mkdir -p /mnt/pistorage
sudo blkid /dev/sda1                    # copy the UUID
```

Add it to `/etc/fstab` **by UUID**, not `/dev/sda1` — device names reorder across reboots and replugs:

```
UUID=<the-uuid>  /mnt/pistorage  ext4  defaults,nofail,noatime  0  2
```

`nofail` stops a missing drive from blocking boot. Then:

```bash
sudo mount -a
sudo chown -R prdx:prdx /mnt/pistorage
sudo systemctl stop pi-storage
rsync -a ~/Documents/Projects/Pi-Storage/photos/ /mnt/pistorage/photos/
```

Set `PHOTOS_DIR=/mnt/pistorage/photos` in `config.env`, then `sudo systemctl start pi-storage` and confirm the gallery still shows everything before deleting the originals.

**Keep the database on the SD card.** `DB_PATH`, `LOG_DIR` and `SECRET_KEY_PATH` default to sitting next to `app.py` — leave them there. SQLite on removable media is a corruption risk, and you want the database readable even when the drive isn't.

**What happens if the drive doesn't mount.** The server refuses to start rather than writing to the SD card underneath the empty mount point:

```
The database lists 214 photos but /mnt/pistorage/photos is empty.
That almost always means the drive holding them is not mounted.
```

That check exists specifically because the silent failure is so bad: photos would land on the card, the gallery would look normal, free space would read as the card's, and you'd discover it when the card filled with the library split across two places.

**Multiple sticks** pooled into one library needs [mergerfs](https://github.com/trapexit/mergerfs) at the filesystem level — it presents several drives as one mount, and losing one drive loses only that drive's files. The app needs no changes; just point `PHOTOS_DIR` at the pooled mount.

**A word on flash sticks.** They have poor write endurance and tend to fail without warning. If the Pi holds the only copy of these photos, an externally-powered USB SSD is meaningfully more reliable for not much more money. Either way, back up — removable storage makes the `rsync` line in Troubleshooting more important, not less.

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
- **413 / "data value transmitted exceeds the capacity limit"** — one request was bigger than `MAX_UPLOAD_MB` (default 2048). If the Shortcut sends the whole selection in a single request, rebuild it with the Repeat-with-Each loop from §6 so each file travels alone. Note: if an old `config.env` still pins `MAX_UPLOAD_MB=200`, that value wins over the new default — raise it there.
- **507** — Pi disk nearly full (below `MIN_FREE_MB`).
- **HEIC rejected on upload** — `pillow-heif` isn't installed; see step 1, or convert to JPEG in the Shortcut. Sign in and open `/health` to see whether the running service has HEIC support.
- **Video tiles show a placeholder instead of a poster** — `ffmpeg` isn't installed (`sudo apt install ffmpeg`, then restart). `/health` reports `video_support`.
- **A video won't play in the browser but downloads fine** — it's HEVC in a browser without HEVC support; see §8.
- **Signup says 503** — `INVITE_CODE` isn't set in `config.env`.
- **Backups** — SD cards fail. Sync the photo folder and the database off the Pi occasionally, e.g. `rsync -a ~/Documents/Projects/Pi-Storage/photos/ ~/Documents/Projects/Pi-Storage/photos.db user@nas:/backup/` (cron it).
