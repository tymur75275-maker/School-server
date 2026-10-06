"""
Кеш таблиць Supabase (Postgres) в оперативній пам'яті.

Весь застосунок читає дані ЛИШЕ з кешу. Запити до Supabase на читання
виконуються тільки при старті процесу (init_cache) і коли адмін натискає
"Оновити з Supabase" (refresh_cache). Зміни через сайт одразу пишуться
і в Supabase, і в кеш.

Щоб app.py / portal.py / шаблони працювали без змін логіки, кеш віддає
записи у тому ж вигляді, що раніше віддавав Airtable:
    {'id': '<рядок>', 'createdTime': '<ISO>', 'fields': {'Назва поля': значення}}
з тими самими назвами полів, зв'язками-списками (['id']) і lookup-полями.
Уся відповідність "поле Airtable <-> колонка Supabase" описана тут, у таблицях
_SIMPLE / _LINK1 / _JUNC нижче.

ВАЖЛИВО: кеш живе в пам'яті ОДНОГО процесу, тож тримайте gunicorn
з --workers 1.
"""

import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

_lock = threading.RLock()

_KEYS = ("users", "students", "subjects", "grades", "discipline", "announcements",
         "messages", "chats", "homework")

BUCKET = "homework"          # приватний Storage-бакет для файлів домашок

_client = None
_last_sync = None
MEDIA_BUCKET = "media"                     # окремий бакет для вкладки «Файли» (фото/відео)
_media = {"groups": {}, "files": {}}      # id -> рядок таблиць media_groups / media_files
_raw = {k: {} for k in _KEYS}     # key -> {id (рядок): рядок таблиці Supabase}
_links = {"ann_to": {}, "ann_read": {}, "hw_students": {}, "subj_teachers": {}, "chat_members": {}}   # parent id -> [child id]
_cache = {k: [] for k in _KEYS}   # key -> список записів у форматі Airtable

# ключ кешу -> таблиця в Supabase
_DB = {
    "users": "users", "students": "students", "subjects": "subjects",
    "grades": "grades", "discipline": "reprimands", "announcements": "announcements",
    "messages": "messages", "chats": "chats", "homework": "homeworks",
}
# таблиці з bigint-ключем (решта — uuid)
_INT_PK = {"grades", "discipline", "announcements", "messages", "homework"}

# звичайні поля: поле Airtable -> колонка
_SIMPLE = {
    "users": {"Full Name": "full_name", "Email": "email", "Role": "role", "Password": "password"},
    "students": {"Ім'я учня": "name", "Клас": "class_name", "Оцінюється": "is_graded"},
    "subjects": {"Назва предмета": "name"},
    "grades": {"Оцінка": "grade", "Коментар вчителя": "teacher_comment",
               "Дата виставлення оцінки": "graded_on", "Статус": "status"},
    "discipline": {"Причина": "reason", "Статус": "status"},
    "announcements": {"Текст": "body", "Обов'язкове": "mandatory", "Термін дії до": "expires_at"},
    "messages": {"Текст": "body"},
    "chats": {"Назва чату": "name", "Група": "is_group"},
    "homework": {"Завдання": "task"},
}
# зв'язки "один запис": поле Airtable (список з одним id) -> колонка-FK
_LINK1 = {
    "students": {"Учень": "user_id"},
    "grades": {"Учень": "student_id", "Предмет": "subject_id"},
    "discipline": {"Учень": "student_id", "Вчитель": "teacher_id"},
    "announcements": {"Від": "from_user_id"},
    "messages": {"Від": "from_user_id", "До": "to_user_id", "Чат": "chat_id"},
    "chats": {"Автор": "created_by"},
}
# зв'язки "багато": поле -> (таблиця-зв'язок, колонка батька, колонка дитини, ключ у _links)
_JUNC = {
    "announcements": {
        "Кому": ("announcement_recipients", "announcement_id", "user_id", "ann_to"),
        "Прочитано": ("announcement_reads", "announcement_id", "user_id", "ann_read"),
    },
    "subjects": {
        "Викладач": ("subject_teachers", "subject_id", "teacher_id", "subj_teachers"),
    },
    "chats": {
        "Учасники": ("chat_members", "chat_id", "user_id", "chat_members"),
    },
    "homework": {
        "Учні": ("homework_students", "homework_id", "student_id", "hw_students"),
    },
}
# поля-вкладення домашок: поле Airtable -> jsonb-колонка
_FILE_COLS = {"Завдання файл": "task_files", "Відповідь файли": "answer_files"}
# після видалення запису ці таблиці могли змінитись у БД (CASCADE / SET NULL) — перечитуємо
_DEPS = {
    "users": ["students", "discipline", "messages", "announcements", "chats"],
    "students": ["grades", "discipline", "homework"],
    "subjects": ["grades"],
    "chats": ["messages"],
}
_DATE_COLS = ("graded_on", "expires_at")


# ---------------------------------------------------------------
# Дрібні допоміжні функції
# ---------------------------------------------------------------

def _s(v):
    return str(v) if v is not None else None


def _pk(key, rid):
    return int(rid) if key in _INT_PK else str(rid)


_FRAC = re.compile(r"\.(\d+)")


def _ts(v):
    """Будь-який timestamptz з Postgres -> '2026-10-05T11:30:00.000000Z' (стабільне сортування)."""
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return str(v)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _put(f, key, val):
    """Як Airtable: порожні значення в fields не потрапляють."""
    if val is None or val == "" or val == []:
        return
    f[key] = val


def _norm_atts(items):
    """Вкладення з jsonb -> список dict з гарантованим 'id'."""
    out = []
    for i, a in enumerate(items or []):
        d = dict(a)
        if not d.get("id"):
            d["id"] = f"legacy{i}"
        out.append(d)
    return out


# ---------------------------------------------------------------
# Завантаження з Supabase
# ---------------------------------------------------------------

def _fetch(table, order):
    """Усі рядки таблиці (з пагінацією — PostgREST віддає максимум 1000 за запит)."""
    out, start = [], 0
    while True:
        q = _client.table(table).select("*")
        for col in order:
            q = q.order(col)
        data = q.range(start, start + 999).execute().data or []
        out.extend(data)
        if len(data) < 1000:
            return out
        start += 1000


def _load(key):
    rows = _fetch(_DB[key], ("created_at", "id"))
    _raw[key] = {str(r["id"]): r for r in rows}
    for jt, pc, cc, store in _JUNC.get(key, {}).values():
        d = {}
        for r in _fetch(jt, (pc, cc)):
            d.setdefault(str(r[pc]), []).append(str(r[cc]))
        _links[store] = d


def init_cache(client):
    """Викликається один раз при старті застосунку. client — supabase.create_client(...)."""
    global _client
    _client = client
    refresh_cache()


def refresh_cache():
    """Повне перезавантаження всіх таблиць із Supabase."""
    global _last_sync
    with _lock:
        for key in _KEYS:
            _load(key)
        _load_media()
        _rebuild()
        _last_sync = datetime.utcnow()
    return _last_sync


def get_last_sync():
    return _last_sync


# ---------------------------------------------------------------
# Побудова записів у форматі Airtable (з lookup-полями)
# ---------------------------------------------------------------

def _rebuild():
    U, S, SB = _raw["users"], _raw["students"], _raw["subjects"]

    def uname(uid):
        return (U.get(uid) or {}).get("full_name")

    def uemail(uid):
        return (U.get(uid) or {}).get("email")

    def rec(rid, row, f):
        return {"id": rid, "createdTime": _ts(row.get("created_at")), "fields": f}

    subj_by_teacher = {}
    for sid in SB:
        for t in _links["subj_teachers"].get(sid, []):
            subj_by_teacher.setdefault(t, []).append(sid)

    users = []
    for uid, u in U.items():
        f = {}
        _put(f, "Full Name", u.get("full_name"))
        _put(f, "Email", u.get("email"))
        _put(f, "Role", u.get("role"))
        _put(f, "Password", u.get("password"))
        _put(f, "Предмети", subj_by_teacher.get(uid))
        users.append(rec(uid, u, f))

    students = []
    for sid, s in S.items():
        uid = _s(s.get("user_id"))
        f = {}
        _put(f, "Ім'я учня", s.get("name"))
        _put(f, "Учень", [uid] if uid else None)
        _put(f, "Email", [uemail(uid)] if uid and uemail(uid) else None)
        _put(f, "Клас", s.get("class_name"))
        if s.get("is_graded"):
            f["Оцінюється"] = True
        students.append(rec(sid, s, f))

    subjects = []
    for sid, s in SB.items():
        tids = [t for t in _links["subj_teachers"].get(sid, []) if t in U]
        f = {}
        _put(f, "Назва предмета", s.get("name"))
        _put(f, "Викладач", tids)
        _put(f, "Email", [uemail(t) for t in tids if uemail(t)])
        subjects.append(rec(sid, s, f))

    grades = []
    for gid, g in _raw["grades"].items():
        stid, sbid = _s(g.get("student_id")), _s(g.get("subject_id"))
        st, sb = S.get(stid), SB.get(sbid)
        su = _s(st.get("user_id")) if st else None
        f = {}
        _put(f, "Учень", [stid] if stid else None)
        _put(f, "Предмет", [sbid] if sbid else None)
        _put(f, "Оцінка", g.get("grade"))
        _put(f, "Коментар вчителя", g.get("teacher_comment"))
        _put(f, "Дата виставлення оцінки", g.get("graded_on"))
        _put(f, "Статус", g.get("status"))
        _put(f, "Назва предмета", [sb["name"]] if sb and sb.get("name") else None)
        _put(f, "Ім'я учня", [st["name"]] if st and st.get("name") else None)
        _put(f, "Email учня", [uemail(su)] if su and uemail(su) else None)
        grades.append(rec(gid, g, f))

    discipline = []
    for did, d in _raw["discipline"].items():
        stid, tid = _s(d.get("student_id")), _s(d.get("teacher_id"))
        st = S.get(stid)
        f = {"№": d["id"]}
        _put(f, "Учень", [stid] if stid else None)
        _put(f, "Вчитель", [tid] if tid else None)
        _put(f, "Причина", d.get("reason"))
        _put(f, "Статус", d.get("status"))
        _put(f, "Ім'я учня", [st["name"]] if st and st.get("name") else None)
        _put(f, "Ім'я вчителя", [uname(tid)] if tid and uname(tid) else None)
        discipline.append(rec(did, d, f))

    announcements = []
    for aid, a in _raw["announcements"].items():
        frm = _s(a.get("from_user_id"))
        to = _links["ann_to"].get(aid, [])
        f = {"№": a["id"]}
        _put(f, "Текст", a.get("body"))
        _put(f, "Від", [frm] if frm else None)
        _put(f, "Ім'я відправника", [uname(frm)] if frm and uname(frm) else None)
        _put(f, "Кому", list(to))
        _put(f, "Імені отримувачів", [uname(x) for x in to if uname(x)])
        if a.get("mandatory"):
            f["Обов'язкове"] = True
        _put(f, "Термін дії до", _ts(a.get("expires_at")))
        _put(f, "Прочитано", list(_links["ann_read"].get(aid, [])))
        announcements.append(rec(aid, a, f))

    messages = []
    for mid, m in _raw["messages"].items():
        frm, to, chat = _s(m.get("from_user_id")), _s(m.get("to_user_id")), _s(m.get("chat_id"))
        f = {"№": m["id"]}
        _put(f, "Текст", m.get("body"))
        _put(f, "Від", [frm] if frm else None)
        _put(f, "До", [to] if to else None)
        _put(f, "Чат", [chat] if chat else None)
        messages.append(rec(mid, m, f))

    chats = []
    for cid, c in _raw["chats"].items():
        f = {}
        _put(f, "Назва чату", c.get("name"))
        if c.get("is_group"):
            f["Група"] = True
        _put(f, "Автор", [_s(c.get("created_by"))] if c.get("created_by") else None)
        _put(f, "Учасники", list(_links["chat_members"].get(cid, [])))
        chats.append(rec(cid, c, f))

    homework = []
    for hid, h in _raw["homework"].items():
        ids = _links["hw_students"].get(hid, [])
        f = {"№": h["id"]}
        _put(f, "Завдання", h.get("task"))
        _put(f, "Учні", list(ids))
        _put(f, "Імені учнів", [S[i]["name"] for i in ids if i in S and S[i].get("name")])
        _put(f, "Email-и учнів",
             [uemail(_s(S[i].get("user_id"))) for i in ids
              if i in S and S[i].get("user_id") and uemail(_s(S[i]["user_id"]))])
        _put(f, "Завдання файл", _norm_atts(h.get("task_files")))
        _put(f, "Відповідь файли", _norm_atts(h.get("answer_files")))
        homework.append(rec(hid, h, f))

    _cache["users"], _cache["students"], _cache["subjects"] = users, students, subjects
    _cache["grades"], _cache["discipline"] = grades, discipline
    _cache["announcements"], _cache["messages"] = announcements, messages
    _cache["chats"], _cache["homework"] = chats, homework


def _find(key, rid):
    rid = str(rid)
    for r in _cache[key]:
        if r["id"] == rid:
            return r
    return None


# ---------------------------------------------------------------
# Читання з кешу
# ---------------------------------------------------------------

def get_users():
    return _cache["users"]


def get_students():
    return _cache["students"]


def get_subjects():
    return _cache["subjects"]


def get_grades():
    return _cache["grades"]


def get_discipline():
    return _cache["discipline"]


def get_announcements():
    return _cache["announcements"]


def get_messages():
    return _cache["messages"]


def get_chats():
    return _cache["chats"]


def get_homework():
    return _cache["homework"]


# ---------------------------------------------------------------
# Запис: поля Airtable -> колонки Supabase
# ---------------------------------------------------------------

def _split(key, fields):
    """Розбиває словник полів (назви Airtable) на: колонки, junction-зв'язки,
    файли, предмети вчителя."""
    row, multi, files, tsubj = {}, {}, {}, None
    simple, link1, junc = _SIMPLE.get(key, {}), _LINK1.get(key, {}), _JUNC.get(key, {})
    for af, val in fields.items():
        if af in simple:
            col = simple[af]
            if val == "" and col in _DATE_COLS:
                val = None
            row[col] = val
        elif af in link1:
            if isinstance(val, (list, tuple)):
                val = val[0] if val else None
            row[link1[af]] = val or None
        elif af in junc:
            multi[af] = [str(x) for x in (val or []) if x]
        elif key == "homework" and af in _FILE_COLS:
            files[_FILE_COLS[af]] = val or []
        elif key == "users" and af == "Предмети":
            tsubj = [str(x) for x in (val or []) if x]
        else:
            raise ValueError(f"Невідоме поле «{af}» для таблиці {key}")
    return row, multi, files, tsubj


def _set_junction(key, rid, af, new_ids):
    jt, pc, cc, store = _JUNC[key][af]
    cur = _links[store].get(rid, [])
    new = list(dict.fromkeys(new_ids))
    add = [x for x in new if x not in cur]
    rem = [x for x in cur if x not in new]
    parent = _pk(key, rid)
    if rem:
        _client.table(jt).delete().eq(pc, parent).in_(cc, rem).execute()
    if add:
        _client.table(jt).insert([{pc: parent, cc: x} for x in add]).execute()
    _links[store][rid] = [x for x in cur if x in new] + add


def _set_teacher_subjects(uid, subject_ids):
    """users.'Предмети' (зв'язок Users<->Предмети) = рядки subject_teachers цього вчителя."""
    ids = set(subject_ids)
    cur = {sid for sid, ts in _links["subj_teachers"].items() if uid in ts}
    rem, add = cur - ids, ids - cur
    if rem:
        _client.table("subject_teachers").delete().eq("teacher_id", uid).in_("subject_id", list(rem)).execute()
    if add:
        _client.table("subject_teachers").insert(
            [{"subject_id": x, "teacher_id": uid} for x in add if x in _raw["subjects"]]).execute()
    for sid in rem:
        _links["subj_teachers"][sid] = [t for t in _links["subj_teachers"].get(sid, []) if t != uid]
    for sid in add:
        if sid in _raw["subjects"]:
            _links["subj_teachers"].setdefault(sid, []).append(uid)


def _apply_files(rid, col, keep_list):
    """Семантика Airtable: список [{'id': ...}] = які вкладення ЗАЛИШИТИ."""
    cur = _norm_atts(_raw["homework"][rid].get(col))
    keep_ids = {a.get("id") for a in (keep_list or []) if isinstance(a, dict)}
    keep = [a for a in cur if a["id"] in keep_ids]
    gone = [a for a in cur if a["id"] not in keep_ids]
    res = _client.table("homeworks").update({col: keep}).eq("id", _pk("homework", rid)).execute().data
    if res:
        _raw["homework"][rid] = res[0]
    _rm_files([a["path"] for a in gone if a.get("path")])


def _apply_extra(key, rid, multi, files, tsubj):
    for af, ids in multi.items():
        _set_junction(key, rid, af, ids)
    for col, keep in files.items():
        _apply_files(rid, col, keep)
    if tsubj is not None:
        _set_teacher_subjects(rid, tsubj)


def _rm_files(paths):
    if not paths:
        return
    for p in paths:
        try:
            os.remove(_fpath(p))
        except OSError:
            pass
    try:
        _client.storage.from_(BUCKET).remove(list(paths))
    except Exception:
        pass            # сирота у Storage не критична для роботи сайту


def _reload_deps(key):
    for dep in _DEPS.get(key, []):
        _load(dep)
    if key == "users":
        _load("subjects")      # перечитує і зв'язки subject_teachers


# ---------------------------------------------------------------
# CRUD-обгортки: пишуть в Supabase і одразу узгоджують кеш.
# ---------------------------------------------------------------

def create_record(table_key, fields):
    with _lock:
        row, multi, files, tsubj = _split(table_key, fields)
        data = _client.table(_DB[table_key]).insert(row).execute().data[0]
        rid = str(data["id"])
        _raw[table_key][rid] = data
        try:
            _apply_extra(table_key, rid, multi, files, tsubj)
        finally:
            _rebuild()
        return _find(table_key, rid)


def batch_create(table_key, records_fields):
    with _lock:
        parts = [_split(table_key, f) for f in records_fields]
        if any(p[1] or p[2] or p[3] is not None for p in parts):
            return [create_record(table_key, f) for f in records_fields]
        data = _client.table(_DB[table_key]).insert([p[0] for p in parts]).execute().data
        for d in data:
            _raw[table_key][str(d["id"])] = d
        _rebuild()
        return [_find(table_key, d["id"]) for d in data]


def update_record(table_key, record_id, fields):
    with _lock:
        rid = str(record_id)
        row, multi, files, tsubj = _split(table_key, fields)
        if row:
            res = _client.table(_DB[table_key]).update(row).eq("id", _pk(table_key, rid)).execute().data
            if not res:
                raise KeyError(f"Запис {rid} не знайдено")
            _raw[table_key][rid] = res[0]
        elif rid not in _raw[table_key]:
            raise KeyError(f"Запис {rid} не знайдено")
        try:
            _apply_extra(table_key, rid, multi, files, tsubj)
        finally:
            _rebuild()
        return _find(table_key, rid)


def delete_record(table_key, record_id):
    with _lock:
        rid = str(record_id)
        paths = []
        if table_key == "homework":
            h = _raw["homework"].get(rid) or {}
            paths = [a["path"] for col in _FILE_COLS.values()
                     for a in _norm_atts(h.get(col)) if a.get("path")]
        _client.table(_DB[table_key]).delete().eq("id", _pk(table_key, rid)).execute()
        _raw[table_key].pop(rid, None)
        for _, _, _, store in _JUNC.get(table_key, {}).values():
            _links[store].pop(rid, None)
        _reload_deps(table_key)
        _rebuild()
        _rm_files(paths)


def batch_delete(table_key, record_ids):
    if not record_ids:
        return
    with _lock:
        ids = [str(i) for i in record_ids]
        _client.table(_DB[table_key]).delete().in_("id", [_pk(table_key, i) for i in ids]).execute()
        for i in ids:
            _raw[table_key].pop(i, None)
            for _, _, _, store in _JUNC.get(table_key, {}).values():
                _links[store].pop(i, None)
        _reload_deps(table_key)
        _rebuild()


# ---------------------------------------------------------------
# Вкладення домашок (Supabase Storage)
# ---------------------------------------------------------------

def _signed(att, seconds=7200):
    if not att.get("path"):
        return ""
    try:
        r = _client.storage.from_(BUCKET).create_signed_url(att["path"], seconds)
    except Exception:
        return ""
    url = (r.get("signedURL") or r.get("signedUrl") or "") if isinstance(r, dict) else ""
    if url.startswith("/"):
        base = os.environ.get("SUPABASE_URL", "").rstrip("/")
        if not url.startswith("/storage/v1"):
            url = "/storage/v1" + url
        url = base + url
    return url


def reload_record(table_key, record_id):
    """Свіжий запис з Supabase. Для домашок вкладення отримують тимчасовий 'url' (2 год)."""
    with _lock:
        rid = str(record_id)
        pk = _pk(table_key, rid)
        res = _client.table(_DB[table_key]).select("*").eq("id", pk).execute().data
        if not res:
            raise KeyError(f"Запис {rid} не знайдено")
        _raw[table_key][rid] = res[0]
        for jt, pc, cc, store in _JUNC.get(table_key, {}).values():
            rows = _client.table(jt).select("*").eq(pc, pk).order(cc).execute().data or []
            _links[store][rid] = [str(r[cc]) for r in rows]
        _rebuild()
        rec = _find(table_key, rid)
        if table_key == "homework":
            fields = dict(rec["fields"])
            for af in _FILE_COLS:
                if af in fields:
                    fields[af] = [dict(a, url=_signed(a)) for a in fields[af]]
            rec = dict(rec, fields=fields)
        return rec


def upload_attachment(table_key, record_id, field, filename, content, content_type):
    """Кладе файл у Storage і додає його у поле-вкладення домашки.
    content — bytes або відкритий файл (rb). Ліміт розміру — з налаштувань Supabase."""
    col = _FILE_COLS[field]
    rid = str(record_id)
    token = secrets.token_hex(12)
    path = f"{rid}/{col}/{token}"          # ASCII-ключ; справжня назва — в jsonb
    if hasattr(content, "seek"):
        content.seek(0, 2)
        size = content.tell()
        content.seek(0)
    else:
        size = len(content)
    _client.storage.from_(BUCKET).upload(
        path, content, {"content-type": content_type or "application/octet-stream",
                        "cache-control": "3600"})   # повторні скачування йдуть з CDN (окремий ліміт cached egress)
    att = {"id": "f" + token, "filename": filename, "path": path,
           "size": size, "type": content_type or "application/octet-stream",
           "uploaded": _ts(datetime.now(timezone.utc))}
    try:
        with _lock:
            row = _raw["homework"].get(rid)
            if row is None:
                raise KeyError(f"Запис {rid} не знайдено")
            new = _norm_atts(row.get(col)) + [att]
            res = _client.table("homeworks").update({col: new}).eq("id", _pk("homework", rid)).execute().data
            if not res:
                raise KeyError(f"Запис {rid} не знайдено")
            _raw["homework"][rid] = res[0]
            _rebuild()
            return _find("homework", rid)
    except Exception:
        _rm_files([path])
        raise


# ---------------------------------------------------------------
# Керування файлами домашок (економія ліміту Storage)
# ---------------------------------------------------------------

def file_report():
    """Усі файли домашок з кешу (без запитів до Supabase)."""
    out = []
    for h in _raw["homework"].values():
        for af, col in _FILE_COLS.items():
            for a in _norm_atts(h.get(col)):
                out.append({"hw": str(h["id"]), "field": af, "id": a["id"], "name": a.get("filename") or "файл",
                            "size": int(a.get("size") or 0), "uploaded": a.get("uploaded"),
                            "stored": bool(a.get("path"))})
    return out


def delete_files(items):
    """items: [(id домашки, поле Airtable-стилю, id вкладення)] — видаляє з БД і зі Storage."""
    by = {}
    for hid, af, att in items:
        by.setdefault((str(hid), af), set()).add(att)
    n = 0
    with _lock:
        for (hid, af), gone in by.items():
            if hid not in _raw["homework"] or af not in _FILE_COLS:
                continue
            cur = _norm_atts(_raw["homework"][hid].get(_FILE_COLS[af]))
            n += sum(1 for a in cur if a["id"] in gone)
            _apply_files(hid, _FILE_COLS[af], [{"id": a["id"]} for a in cur if a["id"] not in gone])
        _rebuild()
    return n


def _list_paths(prefix="", bucket=None):
    out, off = [], 0
    while True:
        items = _client.storage.from_(bucket or BUCKET).list(prefix, {"limit": 1000, "offset": off}) or []
        for it in items:
            p = f"{prefix}/{it['name']}" if prefix else it["name"]
            if it.get("id") is None:
                out += _list_paths(p, bucket)
            else:
                out.append((p, int((it.get("metadata") or {}).get("size") or 0), _ts(it.get("created_at"))))
        if len(items) < 1000:
            return out
        off += 1000


def clean_orphans(bucket=None):
    """Видаляє зі Storage файли, на які немає посилань у БД (молодші за 10 хв не чіпає).
    bucket=None — домашки, bucket=MEDIA_BUCKET — вкладка «Файли»."""
    b = bucket or BUCKET
    with _lock:
        if b == MEDIA_BUCKET:
            used = {f["path"] for f in _media["files"].values()}
        else:
            used = {a["path"] for h in _raw["homework"].values() for col in _FILE_COLS.values()
                    for a in _norm_atts(h.get(col)) if a.get("path")}
        limit = _ts(datetime.now(timezone.utc) - timedelta(minutes=10))
        orph = [(p, sz) for p, sz, cr in _list_paths("", b) if p not in used and (cr or "") < limit]
        for i in range(0, len(orph), 100):
            _client.storage.from_(b).remove([p for p, _ in orph[i:i + 100]])
        return len(orph), sum(sz for _, sz in orph)


# ---------------------------------------------------------------
# Локальний кеш файлів: кожен файл скачується з Supabase один раз,
# далі віддається з диску сервера (не витрачає egress).
# ---------------------------------------------------------------

FILE_CACHE_MB = int(os.environ.get("FILE_CACHE_MB", "300"))
FILE_CACHE_DIR = os.path.join(os.environ.get("TMPDIR") or "/tmp", "school_file_cache")
os.makedirs(FILE_CACHE_DIR, exist_ok=True)


def _fpath(storage_path):
    return os.path.join(FILE_CACHE_DIR, storage_path.replace("/", "_"))


def _evict():
    files = []
    for n in os.listdir(FILE_CACHE_DIR):
        p = os.path.join(FILE_CACHE_DIR, n)
        try:
            st = os.stat(p)
            files.append((st.st_atime, st.st_size, p))
        except OSError:
            pass
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files):                 # спершу найдавніше використані
        if total <= FILE_CACHE_MB * 1048576:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def local_file(att, bucket=None):
    """Шлях до локальної копії вкладення (скачує зі Storage, якщо її ще немає)."""
    sp = att.get("path")
    if not sp:
        return None
    p = _fpath(sp)
    if os.path.exists(p):
        os.utime(p)
        return p
    data = _client.storage.from_(bucket or BUCKET).download(sp)
    tmp = p + "." + secrets.token_hex(4) + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, p)
    _evict()
    return p


# ---------------------------------------------------------------
# Вкладка «Файли»: групи, фото та відео (бакет "media")
# ---------------------------------------------------------------

def _load_media():
    _media["groups"] = {str(r["id"]): r for r in _fetch("media_groups", ("created_at", "id"))}
    _media["files"] = {str(r["id"]): r for r in _fetch("media_files", ("created_at", "id"))}


def get_media_groups():
    return list(_media["groups"].values())


def get_media_files():
    return list(_media["files"].values())


def media_bytes():
    return sum(int(f.get("size") or 0) for f in _media["files"].values())


def create_media_group(name):
    """Нова група; якщо така назва вже є (без урахування регістру) — повертає наявну."""
    name = " ".join((name or "").split())
    with _lock:
        for g in _media["groups"].values():
            if g["name"].lower() == name.lower():
                return g
        row = _client.table("media_groups").insert({"name": name}).execute().data[0]
        _media["groups"][str(row["id"])] = row
        return row


def add_media(group_id, taken_on, filename, content, content_type, kind, user_id=None):
    """Кладе файл у Storage і додає запис. content — відкритий файл (rb) або bytes."""
    group_id = str(group_id)
    if group_id not in _media["groups"]:
        raise KeyError("Групу не знайдено")
    if hasattr(content, "seek"):
        content.seek(0, 2)
        size = content.tell()
        content.seek(0)
    else:
        size = len(content)
    path = f"{group_id}/{secrets.token_hex(12)}"          # ASCII-ключ; справжня назва — в БД
    _client.storage.from_(MEDIA_BUCKET).upload(
        path, content, {"content-type": content_type or "application/octet-stream",
                        "cache-control": "3600"})
    try:
        with _lock:
            row = _client.table("media_files").insert({
                "group_id": group_id, "taken_on": taken_on, "filename": filename, "path": path,
                "kind": kind, "mime": content_type, "size": size,
                "uploaded_by": user_id or None}).execute().data[0]
            _media["files"][str(row["id"])] = row
            return row
    except Exception:
        _rm_media([path])
        raise


def _rm_media(paths):
    for p in paths:
        try:
            os.remove(_fpath(p))
        except OSError:
            pass
    try:
        if paths:
            _client.storage.from_(MEDIA_BUCKET).remove(list(paths))
    except Exception:
        pass


def delete_media(file_id):
    with _lock:
        row = _media["files"].get(str(file_id))
        if not row:
            return
        _client.table("media_files").delete().eq("id", row["id"]).execute()
        _media["files"].pop(str(file_id), None)
        _rm_media([row["path"]])


def delete_media_group(group_id):
    """Видаляє групу разом з усіма її файлами (у БД — каскадом)."""
    gid = str(group_id)
    with _lock:
        paths = [f["path"] for f in _media["files"].values() if str(f["group_id"]) == gid]
        _client.table("media_groups").delete().eq("id", gid).execute()
        _media["groups"].pop(gid, None)
        _media["files"] = {k: v for k, v in _media["files"].items() if str(v["group_id"]) != gid}
        _rm_media(paths)


def media_file(file_id):
    return _media["files"].get(str(file_id))


def delete_media_many(ids):
    n = 0
    with _lock:
        for i in ids:
            if str(i) in _media["files"]:
                delete_media(i)
                n += 1
    return n