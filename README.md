# wa-puppet

WhatsApp Web automation using Python + Playwright with a simple rule engine.

## What this does

- Opens WhatsApp Web in a Chromium browser controlled by Playwright.
- Reuses a local session folder so you do not need to scan QR every run.
- Loads rules from JSON and performs actions for matching senders/messages.

## Project files

- `whatsapp_bot.py`: Main automation script.
- `rules.example.json`: Example rule definitions.
- `requirements.txt`: Python dependencies.

## Conda environment (existing `wa-puppet`)

Use your already-created environment:

```bash
conda activate wa-puppet
pip install -r requirements.txt
playwright install chromium
```

## Configure rules

Create your runtime rules file from the example:

```bash
cp rules.example.json rules.json
```

Then edit `rules.json`.

Example format:

```json
[
	{
		"sender": "+911234567890",
		"action": "auto_reply",
		"reply_text": "Hi, I received your message. I will reply soon.",
		"contains": "urgent"
	},
	{
		"sender": "+911111111111",
		"action": "print"
	}
]
```

### Supported rule fields

- `action` (required): `auto_reply` or `print`.
- `sender` (optional): exact sender header value. For numeric values, comparison is normalized.
- `sender_regex` (optional): regex matched against sender header value.
- `chat_title` (optional): exact chat title match from chat list.
- `chat_title_contains` (optional): partial chat title match.
- `chat_id` (optional): raw chat id from WhatsApp list row attributes (advanced).
- `contains` (optional): only match if incoming message contains this text.
- `reply_text` (required for `auto_reply`): text to send.

All provided fields in a rule must match for that rule to trigger.

## Run

```bash
python whatsapp_bot.py --rules rules.json --poll-seconds 5
```

Use a custom persistent session directory if needed:

```bash
python whatsapp_bot.py --rules rules.json --session-dir .wa_session
```

Persist processed-message state across restarts (default file is `.wa_seen_messages.json`):

```bash
python whatsapp_bot.py --rules rules.json --state-file .wa_seen_messages.json
```

If browser launch fails on macOS, run with explicit Chrome path:

```bash
python whatsapp_bot.py --rules rules.json --browser-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

You can also select channel-based launch (default is `chrome`):

```bash
python whatsapp_bot.py --rules rules.json --channel chrome
```

On first run:

- WhatsApp Web will open.
- Scan QR code.
- Session is saved in `.wa_session/` for future runs.

Optional headless mode:

```bash
python whatsapp_bot.py --rules rules.json --headless
```

## Notes

- WhatsApp Web DOM can change. If selectors break, the script may need updates.
- Script auto-detects local Chrome/Chromium and otherwise uses Playwright browser channel.
- Use responsibly and follow WhatsApp terms/policies.

## Troubleshooting

If you see `ProcessSingleton` / `SingletonLock` errors:

- Another Chrome/bot process is using the same session profile.
- Close all running bot/Chrome instances using that profile.
- Then run again, or use a different session directory:

```bash
python whatsapp_bot.py --rules rules.json --session-dir .wa_session_alt
```

If rules are not matching:

- Run the bot and check log lines like:
	- `Message identifiers | sender=... | title=... | chat_id=...`
- Copy `title` into `chat_title` for reliable matching.
- Prefer `chat_title`/`chat_title_contains` when `sender` phone-number rules do not match.