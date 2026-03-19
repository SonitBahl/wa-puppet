import argparse
import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright


DEFAULT_POLL_SECONDS = 5
WHATSAPP_URL = "https://web.whatsapp.com"
SESSION_DIR = ".wa_session"


def is_session_in_use(session_dir: Path) -> bool:
    """Detect whether another Chrome/Chromium process is using this user-data-dir."""
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False

    needle = f"--user-data-dir={session_dir.resolve()}"
    for line in result.stdout.splitlines():
        if needle in line:
            return True
    return False


def cleanup_stale_session_locks(session_dir: Path) -> None:
    """Remove stale Chromium singleton lock files when profile is not actively used."""
    lock_files = [
        session_dir / "SingletonLock",
        session_dir / "SingletonCookie",
        session_dir / "SingletonSocket",
    ]

    if not any(path.exists() for path in lock_files):
        return

    if is_session_in_use(session_dir):
        return

    for lock_file in lock_files:
        if lock_file.exists():
            try:
                lock_file.unlink()
                logging.info("Removed stale lock file: %s", lock_file)
            except OSError:
                logging.warning("Could not remove lock file: %s", lock_file)


def detect_browser_executable() -> Optional[str]:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() and path.is_file():
            return str(path)
    return None


@dataclass
class Rule:
    action: str
    sender: Optional[str] = None
    sender_regex: Optional[str] = None
    chat_title: Optional[str] = None
    chat_title_contains: Optional[str] = None
    chat_id: Optional[str] = None
    contains: Optional[str] = None
    reply_text: Optional[str] = None


class RuleEngine:
    def __init__(self, rules: List[Rule]) -> None:
        self.rules = rules

    @staticmethod
    def _normalize_sender(sender: str) -> str:
        return re.sub(r"[^0-9+]", "", sender)

    @staticmethod
    def _safe_match_regex(pattern: str, text: str) -> bool:
        try:
            return re.search(pattern, text) is not None
        except re.error:
            return False

    def match(self, sender: str, chat_title: str, chat_id: str, message: str) -> Optional[Rule]:
        sender_normalized = self._normalize_sender(sender)
        for rule in self.rules:
            if rule.sender:
                rule_sender = self._normalize_sender(rule.sender)
                if rule_sender:
                    # Numeric sender rules compare normalized phone-like strings.
                    if rule_sender != sender_normalized:
                        continue
                else:
                    # Non-numeric sender rules compare case-insensitive display strings.
                    if rule.sender.lower() != sender.lower():
                        continue
            if rule.sender_regex and not self._safe_match_regex(rule.sender_regex, sender):
                continue
            if rule.chat_title and rule.chat_title.lower() != chat_title.lower():
                continue
            if rule.chat_title_contains and rule.chat_title_contains.lower() not in chat_title.lower():
                continue
            if rule.chat_id and rule.chat_id != chat_id:
                continue
            if rule.contains and rule.contains.lower() not in message.lower():
                continue
            return rule
        return None


def load_rules(rules_path: Path) -> RuleEngine:
    raw = json.loads(rules_path.read_text(encoding="utf-8"))
    rules = [
        Rule(
            action=item["action"],
            sender=item.get("sender"),
            sender_regex=item.get("sender_regex"),
            chat_title=item.get("chat_title"),
            chat_title_contains=item.get("chat_title_contains"),
            chat_id=item.get("chat_id"),
            contains=item.get("contains"),
            reply_text=item.get("reply_text"),
        )
        for item in raw
    ]
    return RuleEngine(rules)


class WhatsAppBot:
    ROW_SELECTORS = [
        'div[role="listitem"]',
        'div[data-testid="cell-frame-container"]',
        'div[data-testid="chat-list-item"]',
        '[data-id][role="row"]',
    ]

    def __init__(
        self,
        page: Page,
        rules: RuleEngine,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        state_file: str = ".wa_seen_messages.json",
        fallback_visible_chats: int = 3,
    ):
        self.page = page
        self.rules = rules
        self.poll_seconds = poll_seconds
        self.state_file = Path(state_file)
        self.seen_message_keys = self._load_seen_message_keys()
        self.fallback_visible_chats = max(0, fallback_visible_chats)

    def _load_seen_message_keys(self) -> set[str]:
        if not self.state_file.exists():
            return set()
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return {str(x) for x in raw}
        except Exception:
            logging.warning("Could not load state file: %s", self.state_file)
        return set()

    def _save_seen_message_keys(self) -> None:
        try:
            # Keep state bounded to avoid unbounded growth over long runs.
            keys = list(self.seen_message_keys)
            if len(keys) > 5000:
                keys = keys[-5000:]
                self.seen_message_keys = set(keys)
            self.state_file.write_text(json.dumps(keys, indent=2), encoding="utf-8")
        except Exception:
            logging.warning("Could not save state file: %s", self.state_file)

    async def wait_for_login(self) -> None:
        await self.page.goto(WHATSAPP_URL)
        logging.info("Open WhatsApp Web and scan the QR code if asked.")

        await self.page.wait_for_selector("#app", timeout=0)

        while True:
            try:
                counts = await self._selector_counts()
            except PlaywrightError:
                # If the page is closing while we're waiting, let caller handle it.
                raise

            if any(counts.values()):
                break

            logging.info("Waiting for login (QR scan). Chat list not visible yet.")
            await asyncio.sleep(2)

        await asyncio.sleep(1)
        logging.info("WhatsApp Web is ready (chat list detected).")

    async def _get_unread_chats(self) -> List[Dict[str, str]]:
        js = """
        () => {
                        const rowSelectors = [
                            'div[role="listitem"]',
                            'div[data-testid="cell-frame-container"]',
                            'div[data-testid="chat-list-item"]',
                            '[data-id][role="row"]'
                        ];
                        const rows = [];
                        const seen = new Set();
                        for (const selector of rowSelectors) {
                            for (const row of Array.from(document.querySelectorAll(selector))) {
                                if (seen.has(row)) continue;
                                seen.add(row);
                                rows.push(row);
                            }
                        }

                        const getTitle = (row) => {
                            const titleEl = row.querySelector('span[title]');
                            if (titleEl) return titleEl.getAttribute('title') || '';

                            const textEl = row.querySelector('span[dir="auto"], div[dir="auto"] span, [title]');
                            if (textEl) {
                                const t = textEl.getAttribute('title') || textEl.textContent || '';
                                return (t || '').trim();
                            }

                            const aria = row.getAttribute('aria-label') || '';
                            if (aria) return aria.split(',')[0].trim();
                            return '';
                        };

            const unreadChats = [];
            for (const row of rows) {
                                const title = getTitle(row);
                                if (!title) continue;

                const hasUnread =
                    /\\b\\d+\\s+unread\\b/i.test(row.innerText) ||
                    /\\bunread\\b/i.test(row.innerText) ||
                    !!row.querySelector('[aria-label*="unread" i]') ||
                    !!row.querySelector('[data-testid*="unread" i]') ||
                    !!row.querySelector('[data-icon*="unread" i]') ||
                    !!row.querySelector('span[aria-label*="unread" i]');

                if (!hasUnread) continue;

                const chatId =
                    row.getAttribute('data-id') ||
                    row.dataset.id ||
                    row.querySelector('[data-id]')?.getAttribute('data-id') ||
                    '';

                unreadChats.push({ title, chatId });
            }
            return unreadChats;
        }
        """
        result = await self.page.evaluate(js)
        if not isinstance(result, list):
            return []

        chats: List[Dict[str, str]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            chat_id = item.get("chatId")
            if isinstance(title, str):
                chats.append({"title": title, "chat_id": chat_id if isinstance(chat_id, str) else ""})
        return chats

    async def _get_visible_chats(self, limit: int) -> List[Dict[str, str]]:
        js = """
        (limit) => {
                        const rowSelectors = [
                            'div[role="listitem"]',
                            'div[data-testid="cell-frame-container"]',
                            'div[data-testid="chat-list-item"]',
                            '[data-id][role="row"]'
                        ];
                        const rows = [];
                        const seen = new Set();
                        for (const selector of rowSelectors) {
                            for (const row of Array.from(document.querySelectorAll(selector))) {
                                if (seen.has(row)) continue;
                                seen.add(row);
                                rows.push(row);
                            }
                        }

                        const getTitle = (row) => {
                            const titleEl = row.querySelector('span[title]');
                            if (titleEl) return titleEl.getAttribute('title') || '';

                            const textEl = row.querySelector('span[dir="auto"], div[dir="auto"] span, [title]');
                            if (textEl) {
                                const t = textEl.getAttribute('title') || textEl.textContent || '';
                                return (t || '').trim();
                            }

                            const aria = row.getAttribute('aria-label') || '';
                            if (aria) return aria.split(',')[0].trim();
                            return '';
                        };

            const chats = [];
                        for (const row of rows.slice(0, limit)) {
                                const title = getTitle(row);
                                if (!title) continue;

                const chatId =
                    row.getAttribute('data-id') ||
                    row.dataset.id ||
                    row.querySelector('[data-id]')?.getAttribute('data-id') ||
                    '';

                chats.push({ title, chatId });
            }
            return chats;
        }
        """
        result = await self.page.evaluate(js, limit)
        if not isinstance(result, list):
            return []

        chats: List[Dict[str, str]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            chat_id = item.get("chatId")
            if isinstance(title, str):
                chats.append({"title": title, "chat_id": chat_id if isinstance(chat_id, str) else ""})
        return chats

    async def _selector_counts(self) -> Dict[str, int]:
        js = """
        () => {
          const selectors = [
            'div[role="listitem"]',
            'div[data-testid="cell-frame-container"]',
            'div[data-testid="chat-list-item"]',
            '[data-id][role="row"]',
          ];
          const counts = {};
          for (const s of selectors) {
            counts[s] = document.querySelectorAll(s).length;
          }
          return counts;
        }
        """
        result = await self.page.evaluate(js)
        if isinstance(result, dict):
            out: Dict[str, int] = {}
            for key, value in result.items():
                if isinstance(key, str) and isinstance(value, int):
                    out[key] = value
            return out
        return {}

    async def _extract_active_chat_title(self) -> str:
        js = """
        () => {
          const header = document.querySelector('header');
          if (!header) return '';
          const titleEl = header.querySelector('span[title]');
          if (!titleEl) return '';
          return titleEl.getAttribute('title') || '';
        }
        """
        title = await self.page.evaluate(js)
        return title.strip() if isinstance(title, str) else ""

    async def _open_chat_by_title(self, title: str) -> bool:
        clicked = await self.page.evaluate(
            """
            (wantedTitle) => {
              const rowSelectors = [
                'div[role="listitem"]',
                'div[data-testid="cell-frame-container"]',
                'div[data-testid="chat-list-item"]',
                '[data-id][role="row"]'
              ];

              const rows = [];
              const seen = new Set();
              for (const selector of rowSelectors) {
                for (const row of Array.from(document.querySelectorAll(selector))) {
                  if (seen.has(row)) continue;
                  seen.add(row);
                  rows.push(row);
                }
              }

              const getTitle = (row) => {
                const titleEl = row.querySelector('span[title]');
                if (titleEl) return titleEl.getAttribute('title') || '';
                const textEl = row.querySelector('span[dir="auto"], div[dir="auto"] span, [title]');
                if (textEl) {
                  const t = textEl.getAttribute('title') || textEl.textContent || '';
                  return (t || '').trim();
                }
                const aria = row.getAttribute('aria-label') || '';
                if (aria) return aria.split(',')[0].trim();
                return '';
              };

              for (const row of rows) {
                if (getTitle(row) !== wantedTitle) continue;
                row.click();
                return true;
              }
              return false;
            }
            """,
            title,
        )
        if not clicked:
            return False
        await asyncio.sleep(1)
        return True

    async def _extract_sender_from_header(self) -> str:
        js = """
        () => {
          const header = document.querySelector('header');
          if (!header) return '';
          const titleEl = header.querySelector('span[title]');
          if (!titleEl) return '';
          return titleEl.getAttribute('title') || '';
        }
        """
        sender = await self.page.evaluate(js)
        return sender.strip() if isinstance(sender, str) else ""

    async def _extract_last_incoming_message(self) -> str:
        js = """
        () => {
          const incoming = Array.from(document.querySelectorAll('div.message-in'));
          if (!incoming.length) return '';
          const last = incoming[incoming.length - 1];
          const textSpan = last.querySelector('span.selectable-text');
          return textSpan ? textSpan.innerText : '';
        }
        """
        msg = await self.page.evaluate(js)
        return msg.strip() if isinstance(msg, str) else ""

    async def _send_reply(self, text: str) -> None:
        input_selector = "footer div[contenteditable='true']"
        await self.page.wait_for_selector(input_selector)
        await self.page.click(input_selector)
        await self.page.keyboard.type(text)
        await self.page.keyboard.press("Enter")

    async def _run_rule(self, sender: str, message: str, rule: Rule) -> None:
        if rule.action == "print":
            logging.info("Rule action=print sender=%s message=%s", sender, message)
            return

        if rule.action == "auto_reply":
            if not rule.reply_text:
                logging.warning("Rule action=auto_reply missing reply_text for sender=%s", sender)
                return
            await self._send_reply(rule.reply_text)
            logging.info("Auto-replied to sender=%s", sender)
            return

        logging.warning("Unknown action '%s' for sender=%s", rule.action, sender)

    async def process_once(self) -> None:
        chats = await self._get_unread_chats()
        logging.info("Poll tick | unread_chats=%d", len(chats))
        if not chats:
            if self.fallback_visible_chats > 0:
                chats = await self._get_visible_chats(self.fallback_visible_chats)
                logging.info(
                    "Unread detection returned 0; fallback visible chats=%d",
                    len(chats),
                )
            if chats:
                logging.info("Processing fallback visible chats for rule matching")
                # Continue below and process these chats like normal.
            else:
                counts = await self._selector_counts()
                if counts:
                    logging.info("Chat list selector counts: %s", counts)
                active_title = await self._extract_active_chat_title()
                active_sender = await self._extract_sender_from_header()
                if active_title or active_sender:
                    logging.info(
                        "Active chat identifiers | sender=%s | title=%s",
                        active_sender,
                        active_title,
                    )
                return

        for chat in chats:
            title = chat.get("title", "")
            chat_id = chat.get("chat_id", "")
            opened = await self._open_chat_by_title(title)
            if not opened:
                continue

            sender = await self._extract_sender_from_header()
            message = await self._extract_last_incoming_message()
            if not sender or not message:
                continue

            logging.info(
                "Message identifiers | sender=%s | title=%s | chat_id=%s",
                sender,
                title,
                chat_id,
            )

            key = f"{chat_id}::{sender}::{message}"
            if key in self.seen_message_keys:
                continue
            self.seen_message_keys.add(key)
            self._save_seen_message_keys()

            rule = self.rules.match(sender=sender, chat_title=title, chat_id=chat_id, message=message)
            if not rule:
                logging.info("No rule matched sender=%s title=%s", sender, title)
                continue

            await self._run_rule(sender, message, rule)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.process_once()
            except PlaywrightError as exc:
                msg = str(exc)
                if "Target page, context or browser has been closed" in msg:
                    logging.error("Browser was closed; stopping bot loop.")
                    return
                logging.exception("Playwright error while processing messages")
            except Exception:
                logging.exception("Error while processing messages")
            await asyncio.sleep(self.poll_seconds)


async def main() -> None:
    parser = argparse.ArgumentParser(description="WhatsApp Web automation using Playwright")
    parser.add_argument("--rules", default="rules.json", help="Path to JSON rules file")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument(
        "--browser-path",
        default=None,
        help="Optional path to Chrome/Chromium executable",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help="Playwright browser channel (default: chrome)",
    )
    parser.add_argument(
        "--session-dir",
        default=SESSION_DIR,
        help="Persistent browser profile directory (default: .wa_session)",
    )
    parser.add_argument(
        "--state-file",
        default=".wa_seen_messages.json",
        help="File used to persist processed message keys",
    )
    parser.add_argument(
        "--fallback-visible-chats",
        type=int,
        default=3,
        help="When unread detection is zero, process top N visible chats (default: 3, set 0 to disable)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    rules_path = Path(args.rules)
    if not rules_path.exists():
        raise FileNotFoundError(
            f"Rules file not found: {rules_path}. Copy rules.example.json to rules.json and edit it."
        )

    rules = load_rules(rules_path)

    browser_path = args.browser_path or detect_browser_executable()
    if browser_path:
        logging.info("Using browser executable: %s", browser_path)
    else:
        logging.info("No browser path provided. Using Playwright channel: %s", args.channel)

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    session_dir = Path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_session_locks(session_dir)

    context: Optional[BrowserContext] = None
    try:
        async with async_playwright() as pw:
            context_kwargs = {
                "user_data_dir": str(session_dir),
                "headless": args.headless,
                "args": launch_args,
            }
            if browser_path:
                context_kwargs["executable_path"] = browser_path
            else:
                context_kwargs["channel"] = args.channel

            context = await pw.chromium.launch_persistent_context(**context_kwargs)
            page = context.pages[0] if context.pages else await context.new_page()

            bot = WhatsAppBot(
                page=page,
                rules=rules,
                poll_seconds=args.poll_seconds,
                state_file=args.state_file,
                fallback_visible_chats=args.fallback_visible_chats,
            )
            await bot.wait_for_login()
            await bot.run_forever()
    except PlaywrightError as exc:
        err = str(exc)
        if "ProcessSingleton" in err or "SingletonLock" in err:
            raise RuntimeError(
                "Session profile is locked. Close any running bot/Chrome using this session, "
                "or run with a new profile: --session-dir .wa_session_alt"
            ) from exc
        raise RuntimeError(
            "Could not start browser with Playwright. Try --browser-path \"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\""
        ) from exc
    finally:
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                # Context may already be closed during Ctrl+C or manual browser close.
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
