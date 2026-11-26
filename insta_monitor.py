#!/usr/bin/env python3
"""Read-only Instagram new-post monitor for GitHub Actions.

This script DOES NOT like, comment, follow, unfollow, or otherwise modify Instagram.
It checks the authenticated account's home feed and reports unseen posts from accounts
the user follows. Instagram's unofficial endpoints may change or impose restrictions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    FeedbackRequired,
    TwoFactorRequired,
)

STATE_FILE = Path("state.json")
SESSION_FILE = Path("session.json")
MAX_SEEN = 5000
FEED_MAX_AGE_HOURS = int(os.getenv("FEED_MAX_AGE_HOURS", "48"))
REQUEST_TIMEOUT = 20


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"seen": [], "blocked_until": None, "last_success": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root must be an object")
        data.setdefault("seen", [])
        data.setdefault("blocked_until", None)
        data.setdefault("last_success", None)
        return data
    except Exception as exc:
        # Preserve a damaged file for diagnosis; start clean rather than crash-loop.
        STATE_FILE.rename(STATE_FILE.with_name(f"state.corrupt-{int(time.time())}.json"))
        print(f"WARNING: state file was unreadable and has been preserved: {exc}")
        return {"seen": [], "blocked_until": None, "last_success": None}


def save_state(state: dict[str, Any]) -> None:
    state["seen"] = list(dict.fromkeys(str(x) for x in state.get("seen", [])))[-MAX_SEEN:]
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram is not configured; see GitHub Actions run summary/log.")
        return
    body = json.dumps({
        "chat_id": chat_id,
        "text": message[:3900],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API returned: {result}")
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"WARNING: Telegram notification failed: {exc}")


def github_summary(lines: list[str]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def make_client() -> Client:
    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Set IG_USERNAME and IG_PASSWORD in GitHub repository secrets.")

    cl = Client()
    cl.delay_range = [2, 5]

    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(username, password)
            # Validate that the session is still usable.
            cl.get_timeline_feed()
            print("Authenticated using the saved session.")
            return cl
        except LoginRequired:
            print("Saved session expired; attempting a fresh login.")
        except Exception as exc:
            print(f"Saved session could not be reused: {type(exc).__name__}: {exc}")

    cl = Client()
    cl.delay_range = [2, 5]
    totp_seed = os.getenv("IG_TOTP_SEED", "").strip()
    if totp_seed:
        cl.login(username, password, verification_code=cl.totp_generate_code(totp_seed))
    else:
        cl.login(username, password)
    cl.dump_settings(SESSION_FILE)
    print("Fresh login completed and session saved.")
    return cl


def extract_media(feed: Any) -> list[dict[str, Any]]:
    """Normalize common Instagram timeline-feed response shapes."""
    if isinstance(feed, list):
        items = feed
    elif isinstance(feed, dict):
        items = feed.get("feed_items") or feed.get("items") or []
    else:
        items = []

    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        media = item.get("media_or_ad") or item.get("media") or item
        if not isinstance(media, dict):
            continue
        # Exclude ads and non-media feed cards.
        if media.get("pk") is None:
            continue
        result.append(media)
    return result


def media_timestamp(media: dict[str, Any]) -> datetime | None:
    value = media.get("taken_at") or media.get("device_timestamp")
    if value is None:
        return None
    try:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                value = int(value)
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def caption_text(media: dict[str, Any]) -> str:
    caption = media.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "").strip()
    return ""


def permalink(media: dict[str, Any]) -> str:
    code = media.get("code")
    if code:
        return f"https://www.instagram.com/p/{quote(str(code))}/"
    pk = media.get("pk")
    return f"https://www.instagram.com/" if not pk else f"https://www.instagram.com/"


def monitor_once() -> int:
    state = load_state()
    blocked_until = state.get("blocked_until")
    if blocked_until:
        try:
            until = datetime.fromisoformat(blocked_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if utc_now() < until:
                msg = f"Instagram-перевірку призупинено до {until.astimezone().isoformat(timespec='minutes')}."
                print(msg)
                github_summary(["## Instagram monitor", "", msg])
                return 0
        except ValueError:
            state["blocked_until"] = None

    cl = make_client()
    following_ids: set[str] = set()
    cached_ids = {str(x) for x in state.get("following_ids", [])}
    following_ts = state.get("following_ts")
    refresh_following = not cached_ids
    if following_ts:
        try:
            refreshed_at = datetime.fromisoformat(following_ts)
            if refreshed_at.tzinfo is None:
                refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
            refresh_following = refresh_following or (utc_now() - refreshed_at).total_seconds() >= 6 * 3600
        except ValueError:
            refresh_following = True

    if refresh_following:
        try:
            following = cl.user_following(cl.user_id)
            cached_ids = {str(uid) for uid in following.keys()}
            state["following_ids"] = sorted(cached_ids)
            state["following_ts"] = iso_now()
            print(f"Following list refreshed: {len(cached_ids)} accounts")
        except Exception as exc:
            print(f"WARNING: Could not refresh following list: {type(exc).__name__}: {exc}")
    else:
        print(f"Using cached following list: {len(cached_ids)} accounts")

    following_ids = cached_ids

    feed = cl.get_timeline_feed()
    media_items = extract_media(feed)
    seen = set(str(x) for x in state.get("seen", []))
    cutoff = utc_now().timestamp() - FEED_MAX_AGE_HOURS * 3600
    new_posts: list[tuple[dict[str, Any], str]] = []

    for media in media_items:
        pk = str(media.get("pk", ""))
        if not pk or pk in seen:
            continue
        user = media.get("user") or {}
        author_id = str(user.get("pk") or "")
        if following_ids and author_id not in following_ids:
            continue
        ts = media_timestamp(media)
        if ts and ts.timestamp() < cutoff:
            continue
        new_posts.append((media, str(user.get("username") or "невідомий акаунт")))

    # Mark only successfully inspected feed entries as seen, including old entries,
    # to avoid reprocessing the same timeline on every scheduled run.
    for media in media_items:
        pk = media.get("pk")
        if pk is not None:
            state.setdefault("seen", []).append(str(pk))

    state["last_success"] = iso_now()
    state["blocked_until"] = None
    save_state(state)

    if new_posts:
        lines = ["🆕 Нові публікації в Instagram:"]
        summary = ["## Instagram monitor", "", f"Знайдено нових публікацій: **{len(new_posts)}**", ""]
        for media, username in new_posts:
            text = caption_text(media).replace("\n", " ").strip()
            if len(text) > 220:
                text = text[:217] + "..."
            url = permalink(media)
            lines.append(f"@{username}: {text or 'Нова публікація'}\n{url}")
            summary.append(f"- **@{username}** — {text or 'Нова публікація'} ([відкрити]({url}))")
        telegram_send("\n\n".join(lines))
        github_summary(summary)
        print("\n\n".join(lines))
    else:
        message = f"Нових публікацій не знайдено. Перевірено елементів стрічки: {len(media_items)}."
        print(message)
        github_summary(["## Instagram monitor", "", message])

    cl.dump_settings(SESSION_FILE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one read-only check (default).")
    args = parser.parse_args()
    try:
        return monitor_once()
    except (ChallengeRequired, PleaseWaitFewMinutes, RateLimitError, FeedbackRequired) as exc:
        state = load_state()
        # Respect Instagram's restriction signal: pause for six hours, don't retry-loop.
        pause_hours = 6
        state["blocked_until"] = (utc_now().replace(microsecond=0) + __import__("datetime").timedelta(hours=pause_hours)).isoformat()
        save_state(state)
        msg = (f"Instagram повідомив про обмеження/перевірку ({type(exc).__name__}). "
               f"Моніторинг призупинено на {pause_hours} годин; автоматичного обходу немає.")
        print(msg, file=sys.stderr)
        github_summary(["## Instagram monitor — пауза", "", msg])
        try:
            telegram_send(msg)
        except Exception:
            pass
        return 0
    except TwoFactorRequired:
        print("Потрібна двофакторна перевірка. Перевірте вхід у застосунку Instagram.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Fail this run so GitHub Actions marks it red and retries at the next 10-minute slot.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
