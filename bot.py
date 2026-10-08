# -*- coding: utf-8 -*-
"""
Бот сообщества ВК + страничный бот проверки отчётов.

Сообщество:  /+ТОКЕН  -> подключает страницу (запускает страничного бота). Доступно всем.
Страница (работает В ЛЮБОМ ЧАТЕ, где она состоит):
    /отчеты ДД.ММ.ГГГГ   - (владелец страницы и те, кому выдан доступ)
        1) страница пишет /список в специальное сообщество (LIST_GROUP_ID)
        2) ждёт ответ со списком ников и запоминает их (nicks.json)
        3) проверяет обсуждение с отчётами СТРОГО за указанную дату (сутки по МСК)
        4) отвечает по каждому нику:  Nick_Name есть отчет / Nick_Name нет отчета
    /dv                  - (только владелец) ответом на сообщение человека выдаёт ему доступ к /отчеты
    /undv                - (только владелец) ответом на сообщение забирает доступ

pip install vk_api
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.longpoll import VkLongPoll, VkEventType

# ====================== НАСТРОЙКИ ======================
GROUP_TOKEN = "vk1.a.GHSx09IglDrPEe0qC9T-1nRO4OZNX-SrTfzERQa6PSDjwgufKrrSile-Gh38kKSloZGNlxe0YdxRcJnAphHlaUXvMLQDZv9hcRqo5fYQHZVUjYf1YTUZND0xhB8H-oFkYRSlgoyg5O0PQfFD08LjYSqImG8m55jFrZFr0KNtiJtQjHcNQhJ-Rp5ftrmMgWEa728Au7N9V9_SymC5ti-xcw"   # токен бота-сообщества (с правом сообщений)
GROUP_ID = 242102423                  # id сообщества-бота (без минуса)

# Сообщество и обсуждение, где лежат ОТЧЁТЫ
REPORT_GROUP_ID = 221245909           # без минуса
REPORT_TOPIC_ID = 56568576

# Специальное сообщество, которому страница пишет /список и от которого ждёт ответ с никами
LIST_GROUP_ID = 241841230                     # <-- ВПИШИ id этого сообщества (без минуса)
LIST_COMMAND = "/список"
LIST_WAIT = 30                        # сколько секунд ждать ответ на /список
LIST_SETTLE = 3                       # ответ может прийти несколькими сообщениями: ждём тишину столько секунд

REQUIRE_SCREENS = False               # True = отчёт засчитывается, только если есть хотя бы 1 скрин

TZ = timezone(timedelta(hours=3))     # МСК
TOKENS_FILE = "tokens.json"
ACCESS_FILE = "access.json"           # кому владелец выдал доступ через /dv
NICKS_FILE = "nicks.json"             # последний полученный список ников (по владельцу страницы)
# =======================================================

# Ник вида Nick_Name (допускает несколько частей: Ivan_Van_Petrov)
NICK_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z][A-Za-z0-9]*)+\b")
PLACEHOLDER_RE = re.compile(r"\bnick_?name\b", re.IGNORECASE)
PLACEHOLDER_AFTER_RE = re.compile(r"\bnick_?name\b\s*[:\-–—=]\s*(.*)", re.IGNORECASE)
# запасной вариант: "ник: Ivan Petrov" -> Ivan_Petrov
NICK_LABEL_RE = re.compile(
    r"(?:ник(?:нейм)?|nick(?:name)?)\s*[:\-–—]?\s*([A-Za-z]+)[ _]([A-Za-z]+)", re.IGNORECASE)

# упоминания: [id1|Имя], [durov|Имя], @id1, *id1, vk.com/id1, vk.com/durov
MENTION_RE = re.compile(
    r"\[([A-Za-z0-9_.]+)\|[^\]]*\]|[@*]([A-Za-z0-9_.]+)|vk\.com/([A-Za-z0-9_.]+)")
RESERVED_REFS = {"wall", "topic", "away", "app", "album", "photo", "video", "doc",
                 "market", "all", "online", "everyone", "here"}

CMD_REPORT = re.compile(r"^/отчеты\s+(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")
CMD_DV = re.compile(r"^/dv(?:\s+(\S+))?$")
CMD_UNDV = re.compile(r"^/(?:undv|-dv)(?:\s+(\S+))?$")

page_bots = {}          # user_id -> Thread
waiters = {}            # user_id -> ожидание ответа на /список
lock = threading.Lock()
access_lock = threading.Lock()
nicks_lock = threading.Lock()
_resolve_cache = {}


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def norm(s):
    """нижний регистр, без неразрывных пробелов, ё -> е"""
    return (s or "").replace("\xa0", " ").replace("\u200b", "").lower().replace("ё", "е")


# ---------- файлы ----------
def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_token(token):
    tokens = load_tokens()
    if token not in tokens:
        tokens.append(token)
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f)


def _load_access():
    if os.path.exists(ACCESS_FILE):
        with open(ACCESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_access(owner_id):
    with access_lock:
        return set(_load_access().get(str(owner_id), []))


def set_access(owner_id, user_id, grant):
    with access_lock:
        data = _load_access()
        users = set(data.get(str(owner_id), []))
        before = set(users)
        (users.add if grant else users.discard)(user_id)
        data[str(owner_id)] = sorted(users)
        with open(ACCESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return users != before


def save_nicks(owner_id, nicks):
    """Запоминаем последний полученный список ников."""
    with nicks_lock:
        data = {}
        if os.path.exists(NICKS_FILE):
            try:
                with open(NICKS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[str(owner_id)] = {"saved": datetime.now(TZ).isoformat(timespec="seconds"),
                               "nicks": nicks}
        with open(NICKS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)


# ---------- поиск ников и пользователей ----------
def find_nick(text):
    # ссылки и упоминания (vk.com/anna_s, [id1|Имя]) не считаем никами
    text = MENTION_RE.sub(" ", text or "")
    # шаблон отчёта "Nick_Name: Real_Nick" -> берём то, что написано после подписи
    m = PLACEHOLDER_AFTER_RE.search(text)
    if m:
        rest = m.group(1)
        n = NICK_RE.search(rest)
        if n:
            return n.group(0)
        n = re.match(r"\s*([A-Z][A-Za-z0-9]*)\s+([A-Z][A-Za-z0-9]*)", rest)
        if n:
            return f"{n.group(1)}_{n.group(2)}"
    text = PLACEHOLDER_RE.sub(" ", text)       # сама подпись "Nick_Name" ником не считается
    m = NICK_RE.search(text)
    if m:
        return m.group(0)
    m = NICK_LABEL_RE.search(text)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return None


def parse_nick_list(text):
    """Все ники Nick_Name из ответа на /список (без повторов, в исходном порядке)."""
    text = (text or "").replace("<br>", "\n")
    text = MENTION_RE.sub(" ", text)
    seen, result = set(), []
    for m in NICK_RE.finditer(text):
        nick = m.group(0)
        if nick.lower() not in seen:
            seen.add(nick.lower())
            result.append(nick)
    return result


def resolve_user(vk, ref):
    """id123 -> 123, screen_name -> id (через users.get). Сообщества и мусор -> None."""
    ref = (ref or "").strip().rstrip(".")
    m = re.fullmatch(r"id(\d+)", ref, re.IGNORECASE)
    if m:
        return int(m.group(1))
    low = ref.lower()
    if not ref or low in RESERVED_REFS or re.fullmatch(r"(club|public|event)\d+", low):
        return None
    if low in _resolve_cache:
        return _resolve_cache[low]
    uid = None
    try:
        r = vk.users.get(user_ids=ref)
        if r:
            uid = r[0]["id"]
    except Exception:
        pass
    _resolve_cache[low] = uid
    return uid


def ref_to_id(vk, s):
    m = MENTION_RE.search(s or "")
    ref = next((g for g in m.groups() if g), None) if m else (s or "").strip()
    return resolve_user(vk, ref)


# ---------- работа с обсуждениями ----------
def get_comments(vk, group_id, topic_id, since_ts=None, until_ts=None):
    """Комментарии обсуждения (новые -> старые), отсекая по времени: since_ts <= date < until_ts."""
    result, offset = [], 0
    while True:
        r = vk.board.getComments(group_id=group_id, topic_id=topic_id,
                                 count=100, offset=offset, sort="desc")
        items = r.get("items", [])
        if not items:
            break
        stop = False
        for c in items:
            if since_ts and c["date"] < since_ts:
                stop = True
                break
            if until_ts and c["date"] >= until_ts:
                continue
            result.append(c)
        if stop or offset + 100 >= r.get("count", 0):
            break
        offset += 100
        time.sleep(0.34)
    return result


def count_screens(comment):
    n = 0
    for a in comment.get("attachments", []):
        if a.get("type") == "photo":
            n += 1
        elif a.get("type") == "doc" and a["doc"].get("ext", "").lower() in ("png", "jpg", "jpeg", "webp"):
            n += 1
    return n


def collect_reports(vk, start_ts, end_ts):
    """{from_id: {'nicks': set(нижний регистр), 'screens': int}} за [start_ts, end_ts)"""
    data = {}
    comments = get_comments(vk, REPORT_GROUP_ID, REPORT_TOPIC_ID, start_ts, end_ts)
    for c in reversed(comments):
        uid = c.get("from_id", 0)
        if uid <= 0:
            continue
        rec = data.setdefault(uid, {"nicks": set(), "screens": 0})
        nick = find_nick(c.get("text", ""))
        if nick:
            rec["nicks"].add(nick.lower())
        rec["screens"] += count_screens(c)
    return data, len(comments)


# ---------- сообщения ----------
def send(vk, peer_id, text):
    """Отправка с разбивкой по лимиту ВК."""
    while text:
        chunk = text[:3800]
        cut = chunk.rfind("\n") if len(text) > 3800 else len(chunk)
        if cut <= 0:
            cut = len(chunk)
        vk.messages.send(peer_id=peer_id, random_id=0, message=text[:cut])
        text = text[cut:].lstrip("\n")
        time.sleep(0.4)


def request_nick_list(vk, my_id):
    """Пишет /список в специальное сообщество и ждёт ответ. -> текст ответа или None (не дождались)."""
    peer_list = -LIST_GROUP_ID
    w = {"peer": peer_list, "ids": [], "texts": [], "last": 0.0, "event": threading.Event()}
    with lock:
        waiters[my_id] = w
    try:
        vk.messages.send(peer_id=peer_list, random_id=0, message=LIST_COMMAND)
        if not w["event"].wait(LIST_WAIT):
            return None
        # ответ мог прийти несколькими сообщениями: ждём, пока всё придёт
        while time.time() - w["last"] < LIST_SETTLE:
            time.sleep(0.5)
        ids, texts = list(w["ids"]), list(w["texts"])
    finally:
        with lock:
            waiters.pop(my_id, None)

    # longpoll может обрезать длинный текст, поэтому берём полные сообщения через API
    try:
        r = vk.messages.getById(message_ids=",".join(map(str, ids)))
        items = r.get("items") if isinstance(r, dict) else r
        if items:
            items = sorted(items, key=lambda m: m.get("id", 0))
            return "\n".join(m.get("text", "") for m in items)
    except Exception as e:
        log("[page] getById (список):", e)
    return "\n".join(texts)


def run_check(vk, my_id, peer, day, month, year):
    try:
        start = datetime(year, month, day, tzinfo=TZ)
    except ValueError:
        send(vk, peer, "Неверная дата. Формат: /отчеты ДД.ММ.ГГГГ")
        return
    if not LIST_GROUP_ID:
        send(vk, peer, "В коде не указан LIST_GROUP_ID (сообщество, у которого запрашивается /список).")
        return
    start_ts = int(start.timestamp())
    end_ts = int((start + timedelta(days=1)).timestamp())      # строго сутки указанной даты

    # 1) спрашиваем список ников
    send(vk, peer, "Запрашиваю список...")
    try:
        answer = request_nick_list(vk, my_id)
    except Exception as e:
        send(vk, peer, f"Не смог написать в сообщество со списком: {e}")
        return
    if answer is None:
        send(vk, peer, f"Ответ на {LIST_COMMAND} не пришёл за {LIST_WAIT} сек.")
        return

    # 2) запоминаем ники
    nicks = parse_nick_list(answer)
    if not nicks:
        send(vk, peer, "В ответе на /список не нашёл ни одного ника вида Nick_Name.")
        return
    save_nicks(my_id, nicks)

    # 3) проверяем отчёты за эту дату
    send(vk, peer, f"Получил ников: {len(nicks)}. Проверяю отчёты за {day:02d}.{month:02d}.{year}...")
    try:
        reports, _ = collect_reports(vk, start_ts, end_ts)
    except Exception as e:
        send(vk, peer, f"Не смог прочитать обсуждение с отчётами: {e}")
        return

    have = set()
    for rec in reports.values():
        if REQUIRE_SCREENS and rec["screens"] == 0:
            continue
        have |= rec["nicks"]

    # 4) результат
    lines = [f"{n} есть отчет" if n.lower() in have else f"{n} нет отчета" for n in nicks]
    send(vk, peer, "\n".join(lines))


# ---------- команды страницы ----------
def get_reply_author(vk, message_id, peer_id=None, attachments=None):
    """Автор сообщения, на которое ответили командой (два способа + лог)."""
    time.sleep(0.5)
    # способ 1: само сообщение с командой содержит reply_message / fwd_messages
    try:
        r = vk.messages.getById(message_ids=message_id)
        items = r.get("items") if isinstance(r, dict) else r
        if items:
            m = items[0]
            if m.get("reply_message"):
                return m["reply_message"].get("from_id")
            if m.get("fwd_messages"):
                return m["fwd_messages"][0].get("from_id")
        log("[page] /dv: в сообщении нет reply_message/fwd_messages")
    except Exception as e:
        log("[page] /dv getById:", repr(e))
    # способ 2: longpoll отдаёт ссылку на ответ (conversation_message_id) в attachments["reply"]
    try:
        reply = (attachments or {}).get("reply")
        if reply and peer_id:
            cmid = json.loads(reply).get("conversation_message_id")
            if cmid:
                r = vk.messages.getByConversationMessageId(
                    peer_id=peer_id, conversation_message_ids=cmid)
                items = r.get("items") if isinstance(r, dict) else r
                if items:
                    return items[0].get("from_id")
        else:
            log("[page] /dv: longpoll не передал reply, attachments =", attachments)
    except Exception as e:
        log("[page] /dv getByConversationMessageId:", repr(e))
    return None


def handle_message(vk, my_id, sender, peer, message_id, text, check_lock, attachments=None):
    try:
        t = norm(text).strip()
        if not t.startswith("/"):
            return

        grant, revoke = CMD_DV.match(t), CMD_UNDV.match(t)
        if grant or revoke:
            if sender != my_id:                       # выдавать доступ может только владелец
                log(f"[page] /dv/undv проигнорирован: sender={sender}, владелец={my_id}")
                return
            arg = (grant or revoke).group(1)
            target = None
            try:
                target = get_reply_author(vk, message_id, peer, attachments)
            except Exception as e:
                log("[page] /dv ошибка:", repr(e))
            log(f"[page] /dv: sender={sender}, peer={peer}, target={target}")
            if not target and arg:
                target = ref_to_id(vk, arg)
            if not target or target < 0:
                send(vk, peer, "Отправь команду ответом на сообщение человека (или /dv @id123).")
                return
            if target == my_id:
                send(vk, peer, "Это твоя страница, доступ у тебя и так есть.")
                return
            changed = set_access(my_id, target, bool(grant))
            if grant:
                send(vk, peer, f"Доступ к /отчеты выдан: @id{target}" if changed
                     else f"У @id{target} доступ уже есть.")
            else:
                send(vk, peer, f"Доступ к /отчеты забран: @id{target}" if changed
                     else f"У @id{target} и не было доступа.")
            return

        m = CMD_REPORT.match(t)
        if m:
            if sender != my_id and sender not in get_access(my_id):
                return                                # чужим не отвечаем
            d, mo, y = map(int, m.groups())
            if not check_lock.acquire(blocking=False):
                send(vk, peer, "Проверка уже идёт, подожди немного.")
                return
            try:
                run_check(vk, my_id, peer, d, mo, y)
            finally:
                check_lock.release()
    except Exception as e:
        log("[page] ошибка команды:", repr(e))


# ---------- страничный бот ----------
def page_bot(token):
    check_lock = threading.Lock()
    while True:
        try:
            session = vk_api.VkApi(token=token)
            vk = session.get_api()
            my_id = vk.users.get()[0]["id"]
            with lock:
                page_bots[my_id] = threading.current_thread()
            log(f"[page] запущен для id{my_id}")
            longpoll = VkLongPoll(session)
            seen = set()
            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW:
                    continue

                # ответ специального сообщества на /список (входящее сообщение в его диалоге)
                w = waiters.get(my_id)
                if w and event.peer_id == w["peer"] and not getattr(event, "from_me", False):
                    w["ids"].append(event.message_id)
                    w["texts"].append(event.text or "")
                    w["last"] = time.time()
                    w["event"].set()
                    continue

                text = (event.text or "").strip()
                if not text.startswith("/") or event.message_id in seen:
                    continue
                seen.add(event.message_id)
                if len(seen) > 1000:
                    seen.clear()
                # кто написал: я сам (в т.ч. «Избранное») или другой человек (личка/беседа)
                if event.peer_id == my_id or getattr(event, "from_me", False):
                    sender = my_id
                else:
                    sender = event.user_id
                if not sender:
                    continue
                threading.Thread(
                    target=handle_message,
                    args=(vk, my_id, sender, event.peer_id, event.message_id, text, check_lock,
                          getattr(event, "attachments", {})),
                    daemon=True).start()
        except vk_api.exceptions.ApiError as e:
            if e.code == 5:  # токен умер
                log("[page] токен недействителен, останавливаюсь")
                return
            log("[page] ошибка API:", e)
            time.sleep(5)
        except Exception as e:
            log("[page] ошибка:", e)
            time.sleep(5)


def start_page_bot(token):
    vk = vk_api.VkApi(token=token).get_api()
    uid = vk.users.get()[0]["id"]          # проверка токена
    with lock:
        if uid in page_bots and page_bots[uid].is_alive():
            return uid, False
    threading.Thread(target=page_bot, args=(token,), daemon=True).start()
    return uid, True


# ---------- бот сообщества ----------
def main():
    for t in load_tokens():
        try:
            start_page_bot(t)
        except Exception as e:
            log("не удалось поднять сохранённый токен:", e)

    gs = vk_api.VkApi(token=GROUP_TOKEN)
    gvk = gs.get_api()
    lp = VkBotLongPoll(gs, GROUP_ID)
    log("бот сообщества запущен")

    for event in lp.listen():
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue
        msg = event.obj.message
        text = (msg.get("text") or "").strip()
        peer = msg["peer_id"]
        if not text.startswith("/+"):
            continue
        token = text[2:].strip()
        if not token:
            gvk.messages.send(peer_id=peer, random_id=0, message="Использование: /+ТОКЕН")
            continue
        # удаляем сообщение с токеном
        try:
            gvk.messages.delete(peer_id=peer, cmids=msg["conversation_message_id"], delete_for_all=1)
        except Exception:
            pass
        try:
            page_id, started = start_page_bot(token)
            save_token(token)
            answer = (f"Страница id{page_id} подключена. Команды работают в любом чате: /отчеты ДД.ММ.ГГГГ"
                      if started else f"Страница id{page_id} уже подключена.")
        except Exception as e:
            answer = f"Не удалось подключить токен: {e}"
        gvk.messages.send(peer_id=peer, random_id=0, message=answer)


if __name__ == "__main__":
    main()
