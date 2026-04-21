# TradeSync 🤖📈

An AI-powered Telegram signal copier that monitors trading channels, classifies signals using an LLM, and executes trades automatically on cTrader via the Open API — with a human approval fallback for uncertain signals.

Built with Python, Telethon, Groq, and the cTrader Open API.

---

## What It Does

1. **Listens** to 2–3 Telegram channels in real time via Telethon
2. **Classifies** every message (text + images) using an LLM into: Signal / Management / Uncertain / Noise
3. **Validates** signals through a rule engine (SL side, direction, pair resolution)
4. **Executes** confident signals automatically on cTrader (paper mode until app is approved)
5. **Notifies** you via a personal Telegram bot with inline approve/reject buttons for uncertain signals
6. **Logs** everything to a local SQLite database for full auditability

---

## Architecture

```
Telegram Channels (2–3)
        ↓
Layer 0  State Store (SQLite)
Layer 1  Telegram Listener (Telethon)
Layer 2  Preprocessor (edits, albums, replies, reconnect)
Layer 3  LLM Classifier (Groq / swappable)
Layer 4  Rule Validator (post-LLM sanity checks)
Layer 5  Safety Gate (kill switch, duplicates, pause)
Layer 6  cTrader Executor (paper mode / live)
Layer 7  Notification Bot (Telegram inline buttons)
```

---

## Project Structure

```
tradesync/
├── src/
│   ├── bot/
│   │   └── notification_bot.py     # Personal Telegram bot — alerts + commands
│   ├── classifier/
│   │   └── groq_classifier.py      # LLM classifier (swap top 3 lines to change model)
│   ├── executor/
│   │   ├── ctrader_executor.py     # cTrader Open API — paper + live execution
│   │   ├── safety_gate.py          # Pre-execution checks
│   │   └── trade_manager.py        # Management commands (close, SL move)
│   ├── listener/
│   │   └── telegram_listener.py    # Telethon channel listener + preprocessor
│   └── state/
│       └── database.py             # SQLite state store — all tables + helpers
├── downloads/                      # Downloaded images from Telegram
├── .env                            # Credentials (never commit)
├── .gitignore
├── main.py                         # Entry point — runs all components
└── tradesync.db                    # SQLite database (auto-created)
```

---

## Setup

### 1. Clone and create environment

```bash
git clone https://github.com/yourusername/tradesync.git
cd tradesync
python3 -m venv venv
source venv/bin/activate
pip install telethon groq anthropic python-telegram-bot requests python-dotenv aiohttp ctrader-open-api service-identity
```

### 2. Configure credentials

Create a `.env` file in the project root:

```env
# Telegram
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_personal_chat_id

# LLM
GROQ_API_KEY=your_groq_key

# cTrader (Fusion Markets demo)
CTRADER_CLIENT_ID=your_client_id
CTRADER_CLIENT_SECRET=your_client_secret
CTRADER_ACCOUNT_ID=your_account_number
CTRADER_HOST=demo.ctraderapi.com
CTRADER_PORT=5035
CTRADER_ACCESS_TOKEN=your_access_token   # Added when cTrader app is approved

# Risk
RISK_PERCENT=0.02    # 2% per trade (change to 0.20 for 20%)
```

### 3. Get credentials

| Credential | Where to get it |
|---|---|
| Telegram API ID + Hash | [my.telegram.org](https://my.telegram.org) → API Development Tools |
| Telegram Bot Token | [@BotFather](https://t.me/BotFather) → /newbot |
| Telegram Chat ID | [@userinfobot](https://t.me/userinfobot) → /start |
| Groq API Key | [console.groq.com](https://console.groq.com) → API Keys |
| cTrader Client ID/Secret | [openapi.ctrader.com](https://openapi.ctrader.com) → Applications → Credentials |
| cTrader Access Token | [openapi.ctrader.com](https://openapi.ctrader.com) → Applications → Sandbox (available once app is Active) |

### 4. Initialise the database

```bash
python src/state/database.py
```

Output: `✅ Database initialised successfully.`

### 5. Add channels to monitor

Open `src/listener/telegram_listener.py` and edit:

```python
CHANNELS = [
    'channel_username_1',
    'channel_username_2',
]
```

---

## Running

### Terminal 1 — Telegram Listener

```bash
source venv/bin/activate
python -m src.listener.telegram_listener
```

First run will ask for your Telegram phone number and OTP. Session is saved after that.

### Terminal 2 — Notification Bot

```bash
source venv/bin/activate
python -m src.bot.notification_bot
```

Send `/help` to your bot in Telegram to confirm it's running.

---

## Bot Commands

| Command | Action |
|---|---|
| `/kill` | Halt all trade execution immediately |
| `/resume` | Re-enable execution after kill |
| `/risk [%]` | Update risk per trade e.g. `/risk 2` |
| `/status` | System status — kill switch, risk setting |
| `/help` | Command list |

Uncertain signals arrive with **inline buttons** — tap ✅ Approve, ❌ Reject, or ⏸ Pause Channel directly from your phone.

---

## Switching LLM

The classifier is designed to be swapped in 3 lines. Open `src/classifier/groq_classifier.py`:

```python
# ─── LLM CONFIG ──────────────────────────────────────────────
LLM_PROVIDER = "groq"
LLM_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"
client       = Groq(api_key=os.getenv('GROQ_API_KEY'))
# ─────────────────────────────────────────────────────────────
```

To switch to Claude:
```python
LLM_PROVIDER = "claude"
LLM_MODEL    = "claude-haiku-4-5-20251001"
client       = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
```

To switch to OpenAI:
```python
LLM_PROVIDER = "openai"
LLM_MODEL    = "gpt-4o-mini"
client       = openai.OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
```

---

## Going Live (cTrader)

Currently running in **paper mode** — no real trades placed.

To enable live execution:

1. Wait for cTrader app status to change from `Submitted` → `Active` (2–3 business days)
2. Get your access token from [openapi.ctrader.com](https://openapi.ctrader.com) → Sandbox
3. Add `CTRADER_ACCESS_TOKEN` to your `.env`
4. Open `src/executor/ctrader_executor.py` and change:

```python
PAPER_MODE = False
```

---

## Safety Checks

Every signal passes through these checks before execution:

| Check | Behaviour |
|---|---|
| Kill switch | Blocks all execution if active |
| Channel paused | Skips signals from paused channels |
| Duplicate detection | Blocks same pair + direction already open from same channel |
| Missing SL | Routes to uncertain — lot size requires SL |
| Approval timeout | Uncertain signals expire after 10 minutes |

---

## Risk Warning

This system executes trades automatically. Default risk is set to 2% per trade for testing. The original design target of 20% per trade carries significant risk of material account loss from a single misclassified signal.

**Always paper trade for a minimum of 4–6 weeks before deploying real capital.**

---

## Roadmap

- [ ] cTrader live execution (pending app approval)
- [ ] OpenAI / Claude vision for chart image reading
- [ ] Notion ETL pipeline for trade audit log
- [ ] Per-channel trader scoring
- [ ] Backtesting / replay mode on historical messages
- [ ] VPS deployment with systemd watchdog
- [ ] Multi-broker support

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Telegram listener | Telethon (MTProto) |
| LLM classifier | Groq (llama-4-scout) — swappable |
| Notification bot | python-telegram-bot v22 |
| Trade execution | cTrader Open API |
| State store | SQLite via sqlite3 |
| Broker | Fusion Markets (demo) |

---

## Author

Francis — [github.com/francis-plays](https://github.com/francis-plays) | [linkedin.com/in/francis-ukpan-22b788242](https://linkedin.com/in/francis-ukpan-22b788242)