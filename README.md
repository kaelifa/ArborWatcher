# 🌿 ArborWatcher

Monitor and export data from the **Arbor Parent Portal**.  
Built to work with both `.education` and `.sc` URLs, this toolset supports two main modes:

1. **Full Export** — one-off, complete crawl (saves JSON/CSV, optionally zipped)  
2. **Portal Watcher** — periodic monitor that detects changes and sends digests (e.g. to Telegram)

---

## ✅ Folder Structure & File Sanity Check

Make sure your `ArborWatcher/` directory looks like this:

```
ArborWatcher/
├── __pycache__/              # (auto-generated)
├── .github/                  # (contains GitHub Actions workflow)
│   └── workflows/
│       └── arbor-watcher.yml # Automated nightly run configuration
├── .venv/                    # Python virtual environment (optional but recommended)
├── exports/                  # Auto-created folder for saved exports
├── .env                      # Stores your Arbor + Telegram credentials
├── .gitignore
├── arbor_core.py             # Shared crawling + section-fetching helpers
├── arbor_full_export.py      # One-off full export (creates CSV/JSON/ZIP)
├── login_helper.py           # Handles login (uses working guardian login from login_probe.py)
├── monitor_arbor_portal.py   # Continuous watcher + Telegram digest
├── README.md                 # You’re reading it
└── test_env.py               # Optional: quick check that .env loads correctly
```

---

## ⚙️ Environment Setup (`.env` file)

Create a `.env` file in the root folder with your details:

```dotenv
# Arbor login
ARBOR_BASE_URL=https://the-castle-school.uk.arbor.education
ARBOR_EMAIL=you@example.com
ARBOR_PASSWORD=your_password
ARBOR_CHILD_DOB=01/02/2014
ARBOR_LOGIN_METHOD=email

# Optional notifications
TELEGRAM_TOKEN=123:ABC
TELEGRAM_CHAT_ID=123456789

# Optional watcher state file
STATE_FILE=.arbor_state.json
```

---

## 🧰 Setup Commands

Install dependencies:

```bash
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
python3 -m playwright install
```

---

## 🚀 Usage

### 🗂 Full Export

Runs a one-off complete export and optionally creates a zip file.

```bash
python3 arbor_full_export.py --zip
```

### 🔔 Watcher (Change Monitor)

Runs a check across key portal sections and posts a digest via Telegram (if configured in `.env`):

```bash
python3 monitor_arbor_portal.py
```

---

## 🪄 Automate with GitHub Actions

To automate daily checks and notifications, the repo includes a preconfigured workflow:

```
.github/workflows/arbor-watcher.yml
```

It runs automatically at **06:00 UTC (07:00 UK time)** every day and can also be triggered manually in **GitHub → Actions**.

You can adjust the schedule by editing the line inside the workflow:

```yaml
  schedule:
    - cron: "0 6 * * *"   # 06:00 UTC (adjust to your preference)
```

### 🔐 Required GitHub Secrets

Add these under your repo’s **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Description |
|--------------|--------------|
| `ARBOR_BASE_URL` | Your Arbor login URL (e.g. `.education` domain) |
| `ARBOR_EMAIL` | Your Arbor username |
| `ARBOR_PASSWORD` | Your Arbor password |
| `ARBOR_CHILD_DOB` | Optional, for DOB verification |
| `ARBOR_LOGIN_METHOD` | Usually `email`, `microsoft`, or `google` |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `STATE_FILE` | (optional) name of watcher state file |

---

## 🧭 Troubleshooting

| Issue | Fix |
|-------|-----|
| **“You must login to access this page”** | Check `.env` credentials and that `.education` domain is used |
| **Playwright Timeout** | The login form might have changed — rerun `login_probe.py` and update `login_helper.py` |
| **ZIP not created** | Add `--zip` flag to export command |
| **No changes detected in watcher** | Try deleting `.arbor_state.json` to force a fresh baseline |
| **GitHub Action not triggering** | Ensure workflow file is in `.github/workflows/` and Actions are enabled |

---

## 🧾 Version Notes

- **Updated:** November 2025  
- **Python:** 3.11+  
- **Dependencies:** Playwright 1.47+, Requests, Pandas, python-dotenv  
- Uses shared login logic from `login_helper.py`  
- Auto-detects `.education` or `.sc` domains after login  
- Works on macOS, Linux, or Windows  

# 🧩 Workflows Overview

Your repository now includes **two automated GitHub Actions workflows**, both using the same set of secrets.  
These allow Arbor data monitoring and Telegram alerts to run safely in the cloud.

## 🔔 1. `run-watcher.yml` — Full Portal Watcher
Monitors **all major Arbor sections** (messages, communications, trips, payments, etc.) and sends a summary notification when anything changes.

**Schedule:**  
- Runs once per night (or on manual trigger).  

**Secrets used:**  
| Secret | Purpose |
|:--|:--|
| `ARBOR_EMAIL` | Arbor login email |
| `ARBOR_PASSWORD` | Arbor password |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID |
| `ARBOR_BASE_URL` | Optional override (e.g. `https://the-castle-school.uk.arbor.education`) |

**State file:**  
`.arbor_everything_state.json` — cached between runs to detect changes.

---

## 🧮 2. `assignments-watcher.yml` — Homework / Lesson Alerts
Lightweight watcher that focuses on **homework, assignments, and timetable updates**.  
Ideal for near-real-time Telegram notifications.

**Schedule:**  
- Runs every **30 minutes**  
- Can also be triggered manually from the **Actions** tab

**Secrets used (same as above):**  
| Secret | Purpose |
|:--|:--|
| `ARBOR_EMAIL` | Arbor login email |
| `ARBOR_PASSWORD` | Arbor password |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID |
| *(optional)* `ARBOR_BASE_URL` | Fixed base URL for your school portal |

**State file:**  
`.arbor_assignments_state.json` — cached between runs to avoid duplicate Telegram alerts.

---

## 🧠 How to Run Manually

1. Go to your repository on GitHub → **Actions** tab.  
2. Select **“Arbor Assignments Watcher”** or **“Arbor Full Watcher”**.  
3. Click **“Run workflow”** → confirm → it runs instantly in the cloud.  
4. Check Telegram for updates 📨  

---

## 🛠️ Local Testing

```bash
# Install dependencies (once)
python3 -m pip install playwright python-dotenv requests
python3 -m playwright install

# Run locally to check Telegram alerts
python3 monitor_arbor_portal.py --fast
python3 assignments_watcher.py --fast
```

---

**© 2025 ArborWatcher** — created and maintained by Kristina 🌿
