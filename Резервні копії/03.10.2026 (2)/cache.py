"""
Кеш таблиць Airtable в оперативній пам'яті.

Ідея: весь застосунок читає дані ЛИШЕ з цього кешу (_cache).
Запити .all() до Airtable виконуються тільки:
  1) один раз при старті процесу (init_cache),
  2) коли адмін натискає "Оновити з Airtable" (refresh_cache).

Будь-яка зміна (create/update/delete), зроблена через сайт, одразу
пишеться і в Airtable, і в кеш — тож інші користувачі бачать свіжі
дані без додаткового запиту до Airtable.

ВАЖЛИВО: кеш живе в пам'яті ОДНОГО процесу. Якщо на Render буде
запущено кілька gunicorn-воркерів (--workers > 1), у кожного буде
свій окремий кеш, і зміни, зроблені в одному воркері, не з'являться
в іншому, доки хтось не натисне "Оновити". Для такого невеликого
шкільного проєкту тримайте один воркер (--workers 1) — тоді все
буде узгоджено.
"""

import threading
from datetime import datetime

_lock = threading.RLock()

_cache = {
    "users": [],
    "students": [],
    "subjects": [],
    "grades": [],
}

_last_sync = None
_tables = {}


def init_cache(users_table, students_table, subjects_table, grades_table):
    """Викликається один раз при старті застосунку."""
    _tables["users"] = users_table
    _tables["students"] = students_table
    _tables["subjects"] = subjects_table
    _tables["grades"] = grades_table
    refresh_cache()


def refresh_cache():
    """Повне перезавантаження всіх 4 таблиць з Airtable.

    Викликати лише при старті та вручну (кнопка в адмінці) —
    саме це і є ті "зайві запити", яких ми хочемо уникати при
    кожному відкритті сторінки.
    """
    global _last_sync
    with _lock:
        _cache["users"] = _tables["users"].all()
        _cache["students"] = _tables["students"].all()
        _cache["subjects"] = _tables["subjects"].all()
        _cache["grades"] = _tables["grades"].all()
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


# ---------------------------------------------------------------
# CRUD-обгортки: пишуть в Airtable і одразу узгоджують кеш,
# щоб не треба було робити повторний .all() після кожної зміни.
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
            # Якщо запис з якоїсь причини відсутній у кеші — додаємо
            lst.append(rec)
        return rec


def delete_record(table_key, record_id):
    with _lock:
        _tables[table_key].delete(record_id)
        _cache[table_key] = [r for r in _cache[table_key] if r["id"] != record_id]
