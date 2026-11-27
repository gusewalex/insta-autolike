"""
Відкладені лайки нових постів акаунтів, на які ви підписані.

Режими:
  python insta_autolike.py --once        один прохід (для GitHub Actions, режим за замовчуванням)
  python insta_autolike.py --loop        працює постійно (для VPS / власного комп'ютера)
  python insta_autolike.py --login-only  лише увійти й створити session.json

УВАГА: це неофіційна автоматизація, вона порушує правила Instagram.
Є ризик обмежень або блокування акаунта. Використовуйте на власний ризик.
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
)
from instagrapi.extractors import extract_media_v1

# ---------- Налаштування ----------
LIKE_DELAY_MIN = 5              # лайкаємо через стільки хвилин після публікації
LIKE_JITTER_MAX_MIN = 6         # + випадкові 0..N хвилин
MAX_POST_AGE_MIN = 120          # не лайкаємо пости старші за стільки хвилин
MAX_LIKES_PER_HOUR = 20         # запобіжник
SKIP_PROBABILITY = 0.1          # частку постів випадково пропускаємо
QUIET_HOURS = (1, 7)            # з 01:00 до 07:00 (за TZ) не діємо
FOLLOWING_REFRESH_H = 6         # як часто оновлювати список підписок
BLOCK_HOURS = 3                 # пауза після обмеження від Instagram
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "240"))   # скільки один прохід чекає на «дозрілі» лайки
LOOP_POLL_SEC = (180, 360)      # інтервал перевірки стрічки в режимі --loop
LOG_USERNAMES = os.environ.get("LOG_USERNAMES", "0") == "1"     # у публічному репозиторії логи видно всім

SESSION_FILE = Path("session.json")
STATE_FILE = Path("state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autolike")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Стан ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state.setdefault("scheduled", {})
            state.setdefault("done", [])
            return state
        except Exception:
            log.warning("Не вдалося прочитати state.json, починаю з нуля")
    return {"scheduled": {}, "done": []}


def save_state(state: dict) -> None:
    state["done"] = state["done"][-2000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def is_blocked(state: dict) -> bool:
    until = state.get("blocked_until")
    return bool(until) and utcnow() < datetime.fromisoformat(until)


def set_blocked(state: dict, hours: float) -> None:
    state["blocked_until"] = (utcnow() + timedelta(hours=hours)).isoformat()
    save_state(state)


# ---------- Логін ----------
def login() -> Client:
    username = os.environ["IG_USERNAME"]
    password = os.environ["IG_PASSWORD"]
    totp_seed = os.environ.get("IG_TOTP_SEED", "")

    def fresh_client() -> Client:
        c = Client()
        c.delay_range = [2, 6]
        return c

    def code_for(c: Client) -> str:
        return c.totp_generate_code(totp_seed) if totp_seed else ""

    cl = fresh_client()
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed()  # якщо сесія жива, повторний вхід за паролем не потрібен
            cl.dump_settings(SESSION_FILE)
            log.info("Збережена сесія працює, вхід за паролем не потрібен")
            return cl
        except ChallengeRequired:
            raise
        except Exception as e:
            log.warning("Збережена сесія не працює (%s), пробую вхід за паролем", type(e).__name__)
            cl = fresh_client()

    cl.login(username, password, verification_code=code_for(cl))
    cl.dump_settings(SESSION_FILE)
    log.info("Новий вхід виконано, сесію збережено")
    return cl


# ---------- Допоміжне ----------
def get_following(cl: Client, state: dict) -> set[int]:
    ts = state.get("following_ts")
    if ts and state.get("following") and utcnow() - datetime.fromisoformat(ts) < timedelta(hours=FOLLOWING_REFRESH_H):
        return set(state["following"])
    following = cl.user_following(cl.user_id)  # {pk: UserShort}
    state["following"] = [int(pk) for pk in following.keys()]
    state["following_ts"] = utcnow().isoformat()
    log.info("Оновлено список підписок: %d", len(state["following"]))
    return set(state["following"])


def fetch_feed_posts(cl: Client):
    feed = cl.get_timeline_feed()
    for item in feed.get("feed_items", []):
        raw = item.get("media_or_ad")
        if not raw or raw.get("ad_id"):
            continue
        try:
            yield extract_media_v1(raw)
        except Exception:
            continue


def in_quiet_hours() -> bool:
    start, end = QUIET_HOURS
    return start <= datetime.now().hour < end


def poll_feed(cl: Client, following: set[int], state: dict) -> None:
    now = utcnow()
    stats = {"усього": 0, "не від підписок": 0, "вже лайкнуто": 0,
             "старші за ліміт": 0, "пропущено випадково": 0, "вже в черзі/оброблено": 0, "додано в чергу": 0}
    newest_age = None
    for media in fetch_feed_posts(cl):
        stats["усього"] += 1
        pk = str(media.pk)
        age_min = (now - media.taken_at).total_seconds() / 60
        newest_age = age_min if newest_age is None else min(newest_age, age_min)

        if pk in state["scheduled"] or pk in state["done"]:
            stats["вже в черзі/оброблено"] += 1
            continue
        if int(media.user.pk) not in following:
            stats["не від підписок"] += 1
            continue  # рекомендації не від підписок
        if media.has_liked:
            stats["вже лайкнуто"] += 1
            state["done"].append(pk)
            continue
        if age_min > MAX_POST_AGE_MIN:
            stats["старші за ліміт"] += 1
            state["done"].append(pk)
            continue
        if random.random() < SKIP_PROBABILITY:
            stats["пропущено випадково"] += 1
            state["done"].append(pk)
            continue

        due = media.taken_at + timedelta(minutes=LIKE_DELAY_MIN + random.uniform(0, LIKE_JITTER_MAX_MIN))
        due = max(due, now + timedelta(seconds=random.uniform(5, 60)))
        state["scheduled"][pk] = due.isoformat()
        stats["додано в чергу"] += 1
        who = f"@{media.user.username}" if LOG_USERNAMES else "акаунта зі стрічки"
        log.info("У чергу: пост від %s, лайк о %s UTC", who, due.strftime("%H:%M:%S"))

    log.info("Стрічка: %s", ", ".join(f"{k}={v}" for k, v in stats.items()))
    if newest_age is not None:
        log.info("Найновіший пост у стрічці опублікований %.0f хв тому", newest_age)
    save_state(state)


def process_due(cl: Client, state: dict, like_times: list[datetime]) -> None:
    now = utcnow()
    like_times[:] = [t for t in like_times if now - t < timedelta(hours=1)]

    for pk, due_iso in sorted(state["scheduled"].items(), key=lambda kv: kv[1]):
        due = datetime.fromisoformat(due_iso)
        if due > utcnow():
            continue
        if utcnow() - due > timedelta(minutes=MAX_POST_AGE_MIN):
            state["scheduled"].pop(pk)
            state["done"].append(pk)
            continue
        if len(like_times) >= MAX_LIKES_PER_HOUR or in_quiet_hours():
            break
        try:
            ok = cl.media_like(int(pk))
            log.info("Лайк поставлено" if ok else "Лайк не вдався")
        finally:
            state["scheduled"].pop(pk, None)
            state["done"].append(pk)
            save_state(state)
        like_times.append(utcnow())
        time.sleep(random.uniform(5, 20))


def run_pass(cl: Client, state: dict) -> None:
    """Один прохід: оновити стрічку, поставити лайки, що вже «дозріли», і трохи почекати на ті, що ось-ось дозріють."""
    if in_quiet_hours():
        log.info("Тиха година, пропускаю")
        return

    following = get_following(cl, state)
    poll_feed(cl, following, state)

    like_times = [datetime.fromisoformat(t) for t in state.get("like_times", [])]
    deadline = time.time() + RUN_BUDGET_SEC
    while True:
        process_due(cl, state, like_times)
        state["like_times"] = [t.isoformat() for t in like_times]
        save_state(state)

        pending = state["scheduled"]
        if not pending or time.time() >= deadline or len(like_times) >= MAX_LIKES_PER_HOUR:
            break
        next_due = min(datetime.fromisoformat(v) for v in pending.values())
        if (next_due - utcnow()).total_seconds() > deadline - time.time():
            break
        time.sleep(random.uniform(15, 30))


def safe_pass(cl: Client, state: dict) -> Client:
    """Прохід з обробкою типових помилок Instagram. Повертає (можливо, новий) клієнт."""
    try:
        run_pass(cl, state)
    except LoginRequired:
        log.warning("Сесія застаріла, входжу заново")
        SESSION_FILE.unlink(missing_ok=True)
        cl = login()
    except (ChallengeRequired, PleaseWaitFewMinutes, RateLimitError, FeedbackRequired) as e:
        log.error("Instagram обмежує дії (%s). Пауза на %s год.", type(e).__name__, BLOCK_HOURS)
        set_blocked(state, BLOCK_HOURS)
    return cl


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="один прохід (за замовчуванням)")
    mode.add_argument("--loop", action="store_true", help="працювати постійно")
    mode.add_argument("--login-only", action="store_true", help="лише створити session.json")
    args = parser.parse_args()

    if args.login_only:
        login()
        print("Готово: session.json створено.")
        return 0

    state = load_state()
    if is_blocked(state):
        if os.environ.get("FORCE_RUN") == "1":
            log.info("Пауза скасована вручну (FORCE_RUN), продовжую")
            state.pop("blocked_until", None)
        else:
            log.info("Пауза після обмеження діє до %s, пропускаю запуск", state["blocked_until"])
            return 0

    try:
        cl = login()
    except ChallengeRequired:
        log.error("Instagram просить підтвердження входу. Підтвердіть вхід у застосунку Instagram (сповіщення «Це були ви?»). Наступна спроба через 6 год або після ручного запуску з force.")
        set_blocked(state, 6)
        return 0
    except (PleaseWaitFewMinutes, RateLimitError):
        set_blocked(state, BLOCK_HOURS)
        return 0
    except Exception as e:
        log.error(
            "Не вдалося увійти (%s: %s). Імовірно, Instagram не приймає вхід з IP-адреси GitHub. "
            "Створіть сесію на своєму комп'ютері (--login-only) і додайте її в секрет IG_SESSION_B64. "
            "Наступна спроба через %s год.", type(e).__name__, e, BLOCK_HOURS)
        set_blocked(state, BLOCK_HOURS)
        return 0

    if args.loop:
        while not is_blocked(state):
            cl = safe_pass(cl, state)
            time.sleep(random.uniform(*LOOP_POLL_SEC))
        log.info("Пауза після обмеження, зупиняюсь")
    else:
        safe_pass(cl, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
