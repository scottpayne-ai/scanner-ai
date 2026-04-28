# Deploy Scanner AI to Railway
## ~10 minutes. Free tier works to start.

---

### Step 1 — Create a Railway account
Go to https://railway.app and sign up (free, use GitHub login if you have it).

---

### Step 2 — Install Railway CLI (easiest method)
On your computer, open Terminal (Mac) or Command Prompt (Windows) and run:

```
npm install -g @railway/cli
```

Or skip the CLI and use the web dashboard (Step 3b below).

---

### Step 3a — Deploy via CLI (fastest)

```bash
# Navigate to this folder
cd scanner_web

# Login to Railway
railway login

# Create a new project
railway init

# Deploy
railway up
```

Railway will detect Python automatically, install ffmpeg via nixpacks.toml,
and start your server. Takes about 2-3 minutes on first deploy.

---

### Step 3b — Deploy via Web Dashboard (no CLI needed)

1. Go to https://railway.app/new
2. Click "Deploy from GitHub repo" OR "Deploy from local directory"
3. For local: install the Railway GitHub app, push this folder to a GitHub repo, then connect it
4. Railway auto-detects Python and deploys

---

### Step 4 — Get your server URL

After deploy, Railway gives you a URL like:
  https://scanner-ai-production.up.railway.app

Copy that URL. You'll need it in Step 5.

---

### Step 5 — Update your dashboard to point at your server

Open the deployed Scanner AI dashboard and update the API URL.

Or — easier — just re-download the `site/` folder from this zip,
edit line 7 of `app.js`:

  Change:
    const API = '__PORT_8000__'.startsWith('__') ? 'http://localhost:8000' : '__PORT_8000__';
  
  To:
    const API = 'https://YOUR-RAILWAY-URL.up.railway.app';

Then re-upload the site folder to wherever you're hosting the frontend
(Netlify, GitHub Pages, or just open index.html locally).

---

### Step 6 — Add to your phone home screen

On iPhone:
1. Open Safari and go to your dashboard URL
2. Tap the Share button
3. Tap "Add to Home Screen"
4. Name it "Scanner AI"

It will behave like a native app — full screen, no browser chrome.

---

### Costs

Railway free tier: $5/month in free credits (usually enough for light use)
Railway Hobby plan: $5/month flat — unlimited hours, good for 24/7 running

The Whisper model downloads once on first startup (~150MB for "base" model).
After that it's cached and starts instantly.

---

### Environment Variables (optional — your credentials are already in config.yaml)

If you want to keep credentials out of the code, set these in Railway dashboard:
  Settings → Variables → Add Variable

  BCFY_USERNAME = ScottTSU4
  BCFY_PASSWORD = mZTD9ReWfX2N3r!

---

### Troubleshooting

- **Deploy fails on ffmpeg**: Make sure nixpacks.toml is in the folder
- **SSE not connecting**: Check Railway logs — look for "Broadcastify login OK"
- **No calls appearing**: Broadcastify Calls API may need a different endpoint.
  Check logs for poll errors. The system retries automatically.
- **Whisper slow on first call**: Normal — model loads once, then it's fast.
  Upgrade model_size from "base" to "small" in config.yaml for better accuracy.

---

### Files in this package

| File | Purpose |
|------|---------|
| api_server.py | Main backend — runs 24/7 on Railway |
| config.yaml | All settings and credentials |
| requirements.txt | Python dependencies |
| nixpacks.toml | Tells Railway to install ffmpeg |
| railway.json | Railway deployment config |
| Procfile | Fallback start command |
| site/ | Your frontend dashboard files |
| DEPLOY_TO_RAILWAY.md | This guide |
[DEPLOY_TO_RAILWAY.md](https://github.com/user-attachments/files/27177655/DEPLOY_TO_RAILWAY.md)
