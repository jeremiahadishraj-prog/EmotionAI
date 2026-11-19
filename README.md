# EmotionAI: run it on your PC (VS Code + Groq)

```
EmotionAI/
├── frontend/index.html   the whole website (camera + expressions run in the browser)
└── backend/              small Python server that holds your Groq key and answers the chat
```

How it fits together:

```
browser (camera stays here) ──► backend on your PC (Groq key lives here) ──► Groq
```

## What you need
- **Python 3.10 or newer.** Check with `python --version` (or `py --version`). Install from python.org and tick **Add Python to PATH**.
- **VS Code** with the **Python** extension.
- **Chrome or Edge.**
- **A free Groq API key:** sign in at https://console.groq.com, open **API Keys**, click **Create API Key**, and copy the key (it starts with `gsk_`). Keep it private.

## Step 1: Open the project
VS Code → **File → Open Folder…** → choose the extracted **EmotionAI** folder (the one containing `backend` and `frontend`).

## Step 2: Set up the backend (first time only)
Open a terminal: **Terminal → New Terminal**. Then:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

- If activation says *running scripts is disabled*, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and activate again.
- If `python` is not found, use `py` instead of `python`.
- You should see `(.venv)` at the start of the terminal line.
- macOS/Linux: `python3 -m venv .venv`, `source .venv/bin/activate`, `cp .env.example .env`.

## Step 3: Add your Groq key
In the VS Code file list open `backend/.env` and replace the placeholder:

```
GROQ_API_KEY=gsk_your_real_key_here
```

No quotes, no spaces. Save with **Ctrl+S**. Leave the other lines as they are.

## Step 4: Start the backend
In the same terminal:

```powershell
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/api/health in the browser. You must see:

```
{"status":"ok","configured":true,"provider":"groq"}
```

If `configured` is `false`, the key was not saved in `.env`. Leave this terminal running.

## Step 5: Start the website
Open a **second terminal** (click the **+** in the terminal panel), then:

```powershell
cd frontend
python -m http.server 5500
```

Open **http://localhost:5500** in Chrome or Edge. Do not double-click `index.html`; use this address so the server connection and camera work properly.

## Step 6: Check that everything works
1. The status button at the top should say **AI ready**.
2. Open **Companion**, type *I am in mood of crying but I want to be happy*, and press Send. You should get a streamed reply from Groq, and a mood entry appears in **Mood journal**.
3. Open **Live expression** and press **Start camera**. Allow the camera. You should see **Face detected: Yes** and an expression. This runs in your browser and needs no key.

## Every time you come back
Open the folder in VS Code, then run both of these in two terminals:

```powershell
cd backend ; .\.venv\Scripts\Activate.ps1 ; uvicorn main:app --reload --port 8000
cd frontend ; python -m http.server 5500
```

## Troubleshooting
| What you see | Fix |
|---|---|
| Status button says **AI offline** | Backend not running, or wrong address. Check http://localhost:8000/api/health. |
| `configured: false` | `.env` not saved, or the key line is wrong. |
| Chat shows a *key was rejected* message | The Groq key is wrong or was revoked. Create a new one. |
| *Model not found* | Groq renamed a model. Look at https://console.groq.com/docs/models and set `CHAT_MODEL` in `.env`. |
| *Too many requests* | You hit the limit. Wait a minute, or raise `RATE_PER_MINUTE`. Free Groq plans also have their own limits. |
| Browser console says CORS | The address in the browser must match `ALLOWED_ORIGINS` in `.env`, for example `http://localhost:5500`. Restart the backend after editing. |
| Camera blocked | Use http://localhost:5500 (not a preview or file). Click the lock icon, set Camera to Allow, reload. Close Zoom/Teams. |
| `pip` or `uvicorn` not found | The virtual environment is not active. Run the activate command again. |
| Port already in use | Change `8000`/`5500` in the commands (and in `ALLOWED_ORIGINS` for 5500). |

## Changing things
- **Website look and features:** `frontend/index.html`. It is large (about 1 MB) because the face model is embedded, so ask your assistant to edit it, or use Ctrl+F for the text you want to change.
- **Which AI model:** `CHAT_MODEL` and `QUICK_MODEL` in `backend/.env`.
- **Use Anthropic instead:** set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` in `.env`.
- **Limits:** `RATE_PER_MINUTE`, `DAILY_CAP` in `.env`.

## Run the tests
```powershell
cd backend
pip install -r requirements-dev.txt
python -m pytest -q
```
They use a fake AI service, so they do not use your Groq quota.

## Putting it online later
1. Deploy `backend/` (Render, Railway or Fly.io). Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Environment variables: `GROQ_API_KEY`, `ALLOWED_ORIGINS=https://your-site-address`, `TRUST_PROXY=1`.
2. In `frontend/index.html`, set `const DEFAULT_BACKEND='https://your-server-address'` (no slash at the end).
3. Host the `frontend` folder on Netlify, then add its address to `ALLOWED_ORIGINS` and restart the backend.

## Good to know
- Your key is only in `backend/.env`. It is never in the website. `.env` is in `.gitignore`, so it is not uploaded to GitHub.
- Chat text goes from the browser to your server and on to Groq. Camera images never leave the browser.
- Llama models are good in English and Hindi but can be weaker in lower-resource Indian languages than some other AI models. Use the 70B model (`llama-3.3-70b-versatile`) for best results.
- If a model does not follow the reply format, the reply still shows but that message may not produce a mood estimate.
- The expression model estimates how a face looks. It cannot show depression or any condition.
