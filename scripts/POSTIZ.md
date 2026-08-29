# Postiz on Linux server (self-host)

Official install: [Postiz Docker Compose](https://docs.postiz.com/self-host/installation/docker-compose)

Postiz is **not** installed on Windows anymore. Run it on your Linux VPS instead.

---

## 1. Server requirements

- Ubuntu 22.04+ or similar Linux (64-bit)
- **4 GB RAM** minimum (8 GB recommended)
- **20 GB** disk
- Ports **4007** (app) and **8080** (Temporal UI, optional) open in firewall if you need remote access

---

## 2. Install Docker (one-time)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
# Log out and back in (or: newgrp docker) so docker runs without sudo
```

---

## 3. Install Postiz

**Option A — use repo script** (from your machine after git pull):

```bash
bash scripts/setup-postiz.sh
```

**Option B — manual** (on the server):

```bash
git clone https://github.com/gitroomhq/postiz-docker-compose.git ~/postiz-docker-compose
cd ~/postiz-docker-compose

# Generate JWT secret (replace placeholder in docker-compose.yaml)
JWT=$(openssl rand -hex 32)
sed -i "s|JWT_SECRET: 'random string that is unique to every install - just type random characters here!'|JWT_SECRET: '${JWT}'|" docker-compose.yaml

docker compose pull
docker compose up -d
docker compose ps
```

Wait until the `postiz` container is **healthy** (can take 2–5 minutes on first boot).

---

## 4. Open the UI

- Local on server: `http://127.0.0.1:4007`
- From your PC: `http://YOUR_SERVER_IP:4007` (only if port 4007 is open)

```bash
# Quick health check on the server
curl -sI http://127.0.0.1:4007/auth | head -3
```

---

## 5. First-time setup

1. Register the **first user** → becomes org owner  
   (`DISABLE_REGISTRATION=false` by default; set `true` in compose after signup if you want a closed instance)
2. **Settings → Channels** → connect **Instagram**, **Facebook**, **TikTok**  
   Self-host may need provider API keys: [Provider overview](https://docs.postiz.com/self-host/providers/overview)
3. **Settings → API** → create an **API key** (for GitHub Actions later)

---

## 6. Production (recommended)

For anything beyond local testing:

1. Point a domain at the server (e.g. `postiz.yourdomain.com`)
2. Put **HTTPS** in front (Caddy or Nginx) — [reverse proxy docs](https://docs.postiz.com/self-host/reverse-proxies/caddy)
3. Update `MAIN_URL`, `FRONTEND_URL`, `NEXT_PUBLIC_BACKEND_URL` in `docker-compose.yaml` to your HTTPS URL, then:

```bash
cd ~/postiz-docker-compose
docker compose up -d
```

---

## 7. Useful commands

```bash
cd ~/postiz-docker-compose

docker compose ps          # status
docker compose logs -f postiz   # app logs
docker compose restart     # restart stack
docker compose down        # stop
docker compose up -d       # start
```

---

## 8. Hook into YouTube Shorts pipeline

After channels are connected in Postiz:

1. **GitHub Secret:** `POSTIZ_API_KEY` (Settings → Public API in Postiz UI)
2. **Optional GitHub Variables:**
   - `POSTIZ_BASE_URL` — `https://apis.ideationtec.com/blinkviral/app/api/public/v1`
   - `POSTIZ_PLATFORMS` — `instagram,facebook`
   - `POSTIZ_POST_TYPE` — `now` (use `draft` to test without publishing)
   - `POSTIZ_INSTAGRAM_CHANNEL_ID` — from `GET /integrations`
   - `POSTIZ_FACEBOOK_CHANNEL_ID` — from `GET /integrations`
   - `POSTIZ_FACEBOOK_MODE` — `link` (default: caption + YouTube URL) or `video`
3. CI runs after YouTube upload:
   ```bash
   python scripts/upload_postiz.py --kit output/kit.json
   ```

**Publish behavior**

- **Instagram:** native video Reel (uploaded via `/upload`)
- **Facebook (`link` mode):** text caption + `settings.url` = `kit.youtube_url` (no video upload; avoids Meta video-permission errors)
- **Facebook (`video` mode):** native video (requires Meta `pages_manage_posts` video publish access)

**Local test**

```bash
export POSTIZ_API_KEY=your_key
python scripts/upload_postiz.py --list-integrations
python scripts/upload_postiz.py --kit output/kit.json --post-type draft
```

Caption logic matches `upload_drive.py` (title + short body + hashtags from `kit.json`).

Drive → Zapier → Publer remains an optional fallback for TikTok or legacy flows.

---

## Removed from Windows

- WSL Postiz stack stopped and `/root/postiz-docker-compose` deleted
- Windows `portproxy` rules for 4007/8080 cleared
- Windows-only helper scripts removed from `scripts/`
