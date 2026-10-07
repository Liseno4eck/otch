# -*- coding: utf-8 -*-
"""
Бот сообщества ВК + страничный бот проверки отчётов.

Сообщество:  /+ТОКЕН  -> подключает страницу (запускает страничного бота). Доступно всем.
Страница (работает В ЛЮБОМ ЧАТЕ, где она состоит):
    /отчеты ДД.ММ.ГГГГ   - проверка отчётов (владелец страницы и те, кому выдан доступ)
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
GROUP_TOKEN = "vk1.a.GHSx09IglDrPEe0qC9T-1nRO4OZNX-SrTfzERQa6PSDjwgufKrrSile-Gh38kKSloZGNlxe0YdxRcJnAphHlaUXvMLQDZv9hcRqo5fYQHZVUjYf1YTUZND0xhB8H-oFkYRSlgoyg5O0PQfFD08LjYSqImG8m55jFrZFr0KNtiJtQjHcNQhJ-Rp5ftrmMgWEa728Au7N9V9_SymC5ti-xcw"      # токен бота-сообщества (с правом сообщений)
GROUP_ID = 242102423                  # id сообщества-бота (без минуса)

# Сообщество и обсуждение, где лежат ОТЧЁТЫ
REPORT_GROUP_ID = 221245909           # без минуса
REPORT_TOPIC_ID = 56568576

# Сообщество и обсуждение, где лежит "Руководящая администрация"
STAFF_GROUP_ID = 222972502            # без минуса
STAFF_TOPIC_ID = 50373987

TZ = timezone(timedelta(hours=3))     # МСК
ONLY_THAT_DAY = False                 # True = только сутки указанной даты, False = от даты и до сейчас
TOKENS_FILE = "tokens.json"
ACCESS_FILE = "access.json"           # кому владелец выдал доступ через /dv
DEBUG_COUNTS = True                   # True = в конце одной строкой пишет, сколько всего найдено
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

HEADER_RE = re.compile(r"руководящ\w*\s+администрац\w*")
FIRST_RE = re.compile(r"\bзам\w*\.?\s+основател\w*")                 # Заместитель / Зам. основателя
LAST_RE = re.compile(r"\b(?:помощник\w*|пом)\.?\s+основател\w*")     # Помощник основателя

CMD_REPORT = re.compile(r"^/отчеты\s+(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")
CMD_DV = re.compile(r"^/dv(?:\s+(\S+))?$")
CMD_UNDV = re.compile(r"^/(?:undv|-dv)(?:\s+(\S+))?$")

page_bots = {}          # user_id -> Thread
lock = threading.Lock()
access_lock = threading.Lock()
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
    """Комментарии обсуждения (новые -> старые), отсекая по времени."""
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
    """{from_id: {'nick': str|None, 'screens': int}} (в порядке от старых к новым)"""
    data = {}
    comments = get_comments(vk, REPORT_GROUP_ID, REPORT_TOPIC_ID, start_ts, end_ts)
    for c in reversed(comments):
        uid = c.get("from_id", 0)
        if uid <= 0:
            continue
        rec = data.setdefault(uid, {"nick": None, "screens": 0})
        nick = find_nick(c.get("text", ""))
        if nick and not rec["nick"]:
            rec["nick"] = nick
        rec["screens"] += count_screens(c)
    return data, len(comments)


def _has_header(text):
    return any(HEADER_RE.search(norm(l)) for l in (text or "").split("\n"))


def _is_person_line(line):
    """Строка вида '@id1 Nick_Name' / 'Nick_Name' / '[id1|Имя] — Nick': без лишних слов (названий должностей)."""
    nick = find_nick(line)
    if not (MENTION_RE.search(line) or nick):
        return False
    rest = MENTION_RE.sub(" ", line)
    if nick:
        rest = rest.replace(nick, " ")
    return len(re.findall(r"[A-Za-zА-Яа-яЁё]", rest)) < 3


def extract_people(vk, text):
    """Из текста берёт людей от 'Заместитель основателя' до 'Помощник основателя' после заголовка."""
    lines = (text or "").replace("\r", "").split("\n")
    nl = [norm(l) for l in lines]
    h = next((i for i, l in enumerate(nl) if HEADER_RE.search(l)), None)
    if h is None:
        return []
    s = next((i for i in range(h, len(nl)) if FIRST_RE.search(nl[i])), h + 1)
    e = next((i for i in range(s, len(nl)) if LAST_RE.search(nl[i])), len(nl) - 1)
    # под последней должностью может идти несколько людей: берём все строки, где только люди
    j, extra = e + 1, 0
    while j < len(lines) and extra < 40:
        if not lines[j].strip():
            j += 1
            continue
        if _is_person_line(lines[j]):
            e, j, extra = j, j + 1, extra + 1
        else:
            break

    people = []
    pending = None          # индекс человека, у которого есть id, а ник может быть на следующей строке
    for line in lines[s:e + 1]:
        ids = []
        for m in MENTION_RE.finditer(line):
            ref = next(g for g in m.groups() if g)
            uid = resolve_user(vk, ref)
            if uid and uid not in ids:
                ids.append(uid)
        nick = find_nick(line)
        if ids:
            for uid in ids:
                people.append({"id": uid, "nick": nick if len(ids) == 1 else None})
            pending = len(people) - 1 if (len(ids) == 1 and not nick) else None
        elif nick:
            if pending is not None:
                people[pending]["nick"] = nick      # "@id123" и "Nick_Name" на разных строках
                pending = None
            else:
                people.append({"id": None, "nick": nick})
    return people


def parse_staff(vk):
    """-> (people, info). info: сколько комментариев просмотрено, найден ли заголовок, кусок текста."""
    comments = get_comments(vk, STAFF_GROUP_ID, STAFF_TOPIC_ID)      # новые -> старые
    info = {"scanned": len(comments), "header": False, "snippet": ""}
    for i, c in enumerate(comments):
        text = c.get("text", "")
        if not _has_header(text):
            continue
        info["header"] = True
        info["snippet"] = text[:400]
        people = extract_people(vk, text)
        if not people:
            # список мог быть написан следующими комментариями того же автора
            tail, j = text, i - 1
            while j >= 0 and i - j <= 10 and comments[j].get("from_id") == c.get("from_id"):
                tail += "\n" + comments[j].get("text", "")
                j -= 1
            people = extract_people(vk, tail)
        if people:
            seen, uniq = set(), []
            for p in people:
                key = p["id"] or (p["nick"] or "").lower()
                if key not in seen:
                    seen.add(key)
                    uniq.append(p)
            return uniq, info
    return [], info


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


def run_check(vk, my_id, peer, day, month, year):
    try:
        start = datetime(year, month, day, tzinfo=TZ)
    except ValueError:
        send(vk, peer, "Неверная дата. Формат: /отчеты ДД.ММ.ГГГГ")
        return
    start_ts = int(start.timestamp())
    end_ts = int((start + timedelta(days=1)).timestamp()) if ONLY_THAT_DAY else None

    send(vk, peer, "Проверяю отчёты...")
    try:
        reports, n_comments = collect_reports(vk, start_ts, end_ts)
    except Exception as e:
        send(vk, peer, f"Не смог прочитать обсуждение с отчётами: {e}")
        return

    # список руководства нужен и для «настоящих» ников, и для списка не сдавших
    staff, info, staff_err = [], None, None
    try:
        staff, info = parse_staff(vk)
    except Exception as e:
        staff_err = e
    staff_nick = {p["id"]: p["nick"] for p in staff if p["id"] and p["nick"]}

    lines = ["Проверенные отчеты"]
    shown = 0
    for uid, rec in reports.items():
        if not (staff_nick.get(uid) or rec["nick"]) and rec["screens"] == 0:
            continue                                  # не отчёт: ни ника, ни скринов
        nick = staff_nick.get(uid) or rec["nick"] or f"@id{uid}"   # id только если ника нет совсем
        lines.append(f"{nick} {rec['screens']}")
        shown += 1
    if len(lines) == 1:
        lines.append("Отчётов за этот период нет.")
    send(vk, peer, "\n".join(lines))

    if staff_err:
        send(vk, peer, f"Не смог прочитать обсуждение руководства: {staff_err}")
        return
    if not staff:
        if info["scanned"] == 0:
            msg = (f"Обсуждение руководства пустое или недоступно (группа {STAFF_GROUP_ID}, "
                   f"тема {STAFF_TOPIC_ID}). Проверь id и что страница состоит в сообществе.")
        elif not info["header"]:
            msg = (f"Просмотрел {info['scanned']} комментариев, строку «Руководящая администрация» "
                   f"не нашёл. Проверь STAFF_GROUP_ID / STAFF_TOPIC_ID.")
        else:
            msg = ("Строку «Руководящая администрация» нашёл, но людей под ней не распознал. "
                   "Начало текста:\n" + info["snippet"])
        send(vk, peer, msg)
        return

    done_ids = set(reports)
    done_nicks = {r["nick"].lower() for r in reports.values() if r["nick"]}
    done_nicks |= {staff_nick[u].lower() for u in reports if u in staff_nick}
    missing = []
    for p in staff:
        if p["id"] in done_ids:
            continue
        if p["nick"] and p["nick"].lower() in done_nicks:
            continue
        missing.append(p["nick"] or f"@id{p['id']}")               # id только если ника нет
    if missing:
        send(vk, peer, "Не сделаны отчеты:\n" + "\n".join(missing))
    else:
        send(vk, peer, "Не сделаны отчеты: нет, все сдали.")
    if DEBUG_COUNTS:
        send(vk, peer, f"Статистика: комментариев в теме отчётов {n_comments}, отчётов в списке {shown}, "
                       f"людей в списке руководства {len(staff)}, не сдали {len(missing)}.")


# ---------- команды страницы ----------
def get_reply_author(vk, message_id):
    """Автор сообщения, на которое ответили командой."""
    time.sleep(0.5)
    r = vk.messages.getById(message_ids=message_id)
    items = r.get("items") if isinstance(r, dict) else r
    if not items:
        return None
    m = items[0]
    if m.get("reply_message"):
        return m["reply_message"].get("from_id")
    if m.get("fwd_messages"):
        return m["fwd_messages"][0].get("from_id")
    return None


def handle_message(vk, my_id, sender, peer, message_id, text, check_lock):
    try:
        t = norm(text).strip()
        if not t.startswith("/"):
            return

        grant, revoke = CMD_DV.match(t), CMD_UNDV.match(t)
        if grant or revoke:
            if sender != my_id:                       # выдавать доступ может только владелец
                return
            arg = (grant or revoke).group(1)
            target = None
            try:
                target = get_reply_author(vk, message_id)
            except Exception as e:
                log("[page] getById:", e)
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
                    args=(vk, my_id, sender, event.peer_id, event.message_id, text, check_lock),
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
