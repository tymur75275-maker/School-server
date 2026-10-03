"""
Кеш таблиць Airtable в оперативній пам'яті.

Весь застосунок читає дані ЛИШЕ з кешу. Запити .all() до Airtable
виконуються тільки при старті процесу (init_cache) і коли адмін натискає
"Оновити з Airtable" (refresh_cache). Зміни через сайт одразу пишуться
і в Airtable, і в кеш.

ВАЖЛИВО: кеш живе в пам'яті ОДНОГО процесу, тож тримайте gunicorn
з --workers 1.
"""

import threading
from datetime import datetime

_lock = threading.RLock()

_KEYS = ("users", "students", "subjects", "grades", "discipline", "announcements",
         "messages", "chats", "homework")
_cache = {k: [] for k in _KEYS}
_last_sync = None
_tables = {}


def init_cache(users_table, students_table, subjects_table, grades_table,
               discipline_table=None, announcements_table=None,
               messages_table=None, chats_table=None, homework_table=None):
    """Викликається один раз при старті застосунку."""
    _tables["users"] = users_table
    _tables["students"] = students_table
    _tables["subjects"] = subjects_table
    _tables["grades"] = grades_table
    if discipline_table is not None:
        _tables["discipline"] = discipline_table
    if announcements_table is not None:
        _tables["announcements"] = announcements_table
    for key, tbl in (("messages", messages_table), ("chats", chats_table),
                     ("homework", homework_table)):
        if tbl is not None:
            _tables[key] = tbl
    refresh_cache()


def refresh_cache():
    """Повне перезавантаження всіх підключених таблиць з Airtable."""
    global _last_sync
    with _lock:
        for key, table in _tables.items():
            _cache[key] = table.all()
        _last_sync = datetime.utcnow()
    return _last_sync


def get_last_sync():
    return _last_sync


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
# CRUD-обгортки: пишуть в Airtable і одразу узгоджують кеш.
# ---------------------------------------------------------------

def create_record(table_key, fields):
    with _lock:
        rec = _tables[table_key].create(fields)
        _cache[table_key].append(rec)
        return rec


def batch_create(table_key, records_fields):
    with _lock:
        created = _tables[table_key].batch_create(records_fields)
        _cache[table_key].extend(created)
        return created


def update_record(table_key, record_id, fields):
    with _lock:
        rec = _tables[table_key].update(record_id, fields)
        lst = _cache[table_key]
        for i, r in enumerate(lst):
            if r["id"] == record_id:
                lst[i] = rec
                break
        else:
            lst.append(rec)
        return rec


def delete_record(table_key, record_id):
    with _lock:
        _tables[table_key].delete(record_id)
        _cache[table_key] = [r for r in _cache[table_key] if r["id"] != record_id]


def reload_record(table_key, record_id):
    """Свіжий запис з Airtable (потрібно для вкладень: їхні URL діють ~2 год)."""
    with _lock:
        rec = _tables[table_key].get(record_id)
        lst = _cache[table_key]
        for i, r in enumerate(lst):
            if r["id"] == record_id:
                lst[i] = rec
                break
        else:
            lst.append(rec)
        return rec


def upload_attachment(table_key, record_id, field, filename, content, content_type):
    """Завантажує файл у поле-вкладення (pyairtable >= 3.0, до 5 МБ на файл)."""
    with _lock:
        _tables[table_key].upload_attachment(
            record_id, field, filename, content=content, content_type=content_type)
        return reload_record(table_key, record_id)