"""Вкладки кабінетів: Статистика, Догани, Оголошення (етап 2)."""
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import os
import re
import secrets
import tempfile

from flask import Blueprint, request, session, jsonify, redirect, send_file

import cache

bp = Blueprint('portal', __name__)
TZ = ZoneInfo('Europe/Kyiv')
ROLE_LABEL = {'admin': 'Адмін', 'teacher': 'Вчитель', 'child': 'Учень'}


# ---------- допоміжні ----------
def cv(v):
    if isinstance(v, list):
        return v[0] if v else ''
    return v if v is not None else ''


def lst(v):
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)] if v else []


def err(msg, code=400):
    return jsonify(ok=False, error=msg), code


def _em(rec_fields, key='Email'):
    return str(cv(rec_fields.get(key)) or '').strip().lower()


def me_record(email):
    for u in cache.get_users():
        if _em(u['fields']) == email:
            return u


def student_record(email):
    """Запис учня: за зв'язком Учень→Users, потім за Email-lookup, потім за іменем."""
    me = me_record(email)
    students = cache.get_students()
    if me:
        for s in students:
            if me['id'] in lst(s['fields'].get('Учень')):
                return s
    for s in students:
        if _em(s['fields']) == email:
            return s
    if me:
        name = str(cv(me['fields'].get('Full Name')) or '').strip()
        for s in students:
            if name and str(cv(s['fields'].get("Ім'я учня")) or '').strip() == name:
                return s


def student_name(email, st):
    if st and cv(st['fields'].get("Ім'я учня")):
        return str(cv(st['fields'].get("Ім'я учня")))
    me = me_record(email)
    return str(cv(me['fields'].get('Full Name')) or '') if me else ''


def parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def local_to_utc(s):
    """'2026-10-05T14:30' (час Києва) -> ISO UTC; порожнє -> None."""
    if not s:
        return None
    d = datetime.fromisoformat(s).replace(tzinfo=TZ)
    return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def login_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if 'user' not in session:
            return err('Потрібен вхід', 401)
        return fn(*a, **k)
    return w


def staff_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if session.get('role') not in ('teacher', 'admin'):
            return err('Доступ заборонено', 403)
        return fn(*a, **k)
    return w


# ---------- оголошення ----------
def purge_expired_announcements():
    now = datetime.now(timezone.utc)
    for r in list(cache.get_announcements()):
        exp = parse_dt(r['fields'].get('Термін дії до'))
        if exp and exp <= now:
            try:
                cache.delete_record('announcements', r['id'])
            except Exception:
                pass


def build_announcements(role, me_id):
    purge_expired_announcements()
    names = user_names()
    out = []
    for r in cache.get_announcements():
        f = r['fields']
        to_ids = lst(f.get('Кому'))
        for_me = me_id in to_ids
        if role == 'student' and not for_me:
            continue
        exp = parse_dt(f.get('Термін дії до'))
        loc = exp.astimezone(TZ) if exp else None
        read_ids = lst(f.get('Прочитано'))
        out.append({
            'id': r['id'], 'num': cv(f.get('№')) or 0,
            'text': str(f.get('Текст') or ''),
            'from_name': str(cv(f.get("Ім'я відправника")) or ''),
            'to_ids': to_ids, 'to_names': lst(f.get('Імені отримувачів')),
            'mandatory': bool(f.get("Обов'язкове")),
            'exp_label': loc.strftime('%d.%m.%Y %H:%M') if loc else '',
            'exp_input': loc.strftime('%Y-%m-%dT%H:%M') if loc else '',
            'for_me': for_me, 'read': me_id in read_ids,
            'read_count': len(read_ids),
            'read_names': [names.get(i, '?') for i in read_ids],
            'unread_names': [names.get(i, '?') for i in to_ids if i not in read_ids],
            'created': _local(r.get('createdTime'), True),
        })
    out.sort(key=lambda a: a['num'] if isinstance(a['num'], (int, float)) else 0, reverse=True)
    return out


@bp.route('/announcements/save', methods=['POST'])
@login_only
@staff_only
def ann_save():
    f = request.form
    text = f.get('text', '').strip()
    to = [x for x in f.getlist('to') if x]
    if not text:
        return err('Введіть текст оголошення')
    if not to:
        return err('Оберіть отримувачів')
    try:
        exp = local_to_utc(f.get('expires', '').strip())
    except ValueError:
        return err('Некоректна дата')
    if exp and parse_dt(exp) <= datetime.now(timezone.utc):
        return err('Термін дії вже минув')
    fields = {'Текст': text, 'Кому': to, "Обов'язкове": f.get('mandatory') == 'on'}
    ann_id = f.get('id', '').strip()
    try:
        if ann_id:
            fields['Термін дії до'] = exp          # None очищає поле
            fields['Прочитано'] = []               # після редагування читають знову
            cache.update_record('announcements', ann_id, fields)
        else:
            me = me_record(str(session['user']).strip().lower())
            if me:
                fields['Від'] = [me['id']]
            if exp:
                fields['Термін дії до'] = exp
            cache.create_record('announcements', fields)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/announcements/delete', methods=['POST'])
@login_only
@staff_only
def ann_delete():
    try:
        cache.delete_record('announcements', request.form.get('id', ''))
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/announcements/read', methods=['POST'])
@login_only
def ann_read():
    me = me_record(str(session['user']).strip().lower())
    rid = request.form.get('id', '')
    rec = next((r for r in cache.get_announcements() if r['id'] == rid), None)
    if not rec or not me:
        return err('Не знайдено', 404)
    read = lst(rec['fields'].get('Прочитано'))
    if me['id'] not in read:
        try:
            cache.update_record('announcements', rid, {'Прочитано': read + [me['id']]})
        except Exception as e:
            return err(str(e), 500)
    return jsonify(ok=True)


# ---------- догани ----------
def build_discipline(role, st_rec):
    out = []
    for r in cache.get_discipline():
        f = r['fields']
        st_ids = lst(f.get('Учень'))
        if role == 'student' and not (st_rec and st_rec['id'] in st_ids):
            continue
        out.append({
            'id': r['id'], 'num': cv(f.get('№')) or 0,
            'student_id': st_ids[0] if st_ids else '',
            'student_name': str(cv(f.get("Ім'я учня")) or ''),
            'teacher_name': str(cv(f.get("Ім'я вчителя")) or ''),
            'reason': str(f.get('Причина') or ''),
            'status': str(cv(f.get('Статус')) or 'Не прочитано'),
        })
    out.sort(key=lambda d: d['num'] if isinstance(d['num'], (int, float)) else 0, reverse=True)
    return out


@bp.route('/discipline/save', methods=['POST'])
@login_only
@staff_only
def disc_save():
    f = request.form
    st_id = f.get('student_id', '').strip()
    reason = f.get('reason', '').strip()
    status = f.get('status', 'Не прочитано')
    if status not in ('Не прочитано', 'Прочитано'):
        status = 'Не прочитано'
    if not st_id or not reason:
        return err('Оберіть учня та вкажіть причину')
    try:
        rid = f.get('id', '').strip()
        if rid:
            cache.update_record('discipline', rid,
                                {'Учень': [st_id], 'Причина': reason, 'Статус': status})
        else:
            me = me_record(str(session['user']).strip().lower())
            fields = {'Учень': [st_id], 'Причина': reason, 'Статус': 'Не прочитано'}
            if me:
                fields['Вчитель'] = [me['id']]
            cache.create_record('discipline', fields)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/discipline/delete', methods=['POST'])
@login_only
@staff_only
def disc_delete():
    try:
        cache.delete_record('discipline', request.form.get('id', ''))
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/discipline/read', methods=['POST'])
@login_only
def disc_read():
    if session.get('role') != 'student':
        return err('Доступ заборонено', 403)
    st = student_record(str(session['user']).strip().lower())
    rid = request.form.get('id', '')
    rec = next((r for r in cache.get_discipline() if r['id'] == rid), None)
    if not rec or not st or st['id'] not in lst(rec['fields'].get('Учень')):
        return err('Не знайдено', 404)
    try:
        cache.update_record('discipline', rid, {'Статус': 'Прочитано'})
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


# ---------- статистика ----------
def _nums(s):
    out = []
    for t in str(s or '').replace(';', ',').split(','):
        try:
            out.append(float(t.strip()))
        except ValueError:
            pass
    return out


def build_stats(role, email, st_rec):
    name = str(cv(st_rec['fields'].get("Ім'я учня")) or '') if st_rec else ''
    agg = {}
    gids = {s['id'] for s in cache.get_students() if s['fields'].get('Оцінюється')}
    mine = set(staff_subject_names(role, email)) if role == 'teacher' else None   # вчитель бачить лише свої предмети
    for rec in cache.get_grades():
        f = rec['fields']
        if not gids.intersection(f.get('Учень') or []):
            continue          # статистика лише по учнях з галочкою «Оцінюється»
        st = str(cv(f.get("Ім'я учня")) or '')
        if role == 'student' and not (_em(f, 'Email учня') == email or (name and st == name)):
            continue
        subj = str(cv(f.get('Назва предмета')) or cv(f.get('Предмет')) or '')
        d = str(cv(f.get('Дата виставлення оцінки')) or cv(f.get('Дата')) or '')
        if mine is not None and subj not in mine:
            continue
        nums = _nums(cv(f.get('Оцінка')))
        if not (st and subj and d and nums):
            continue
        agg.setdefault((st, subj, d), []).extend(nums)
    return [{'s': k[0], 'sub': k[1], 'd': k[2], 'v': round(sum(v) / len(v), 2)}
            for k, v in sorted(agg.items(), key=lambda x: x[0][2])]


def staff_subject_names(role, email):
    names = set()
    for s in cache.get_subjects():
        f = s['fields']
        n = str(cv(f.get('Назва предмета')) or '')
        if n and (role == 'admin' or email in [e.strip().lower() for e in lst(f.get('Email'))]):
            names.add(n)
    return sorted(names)


# ---------- чати ----------
def user_names():
    return {u['id']: str(cv(u['fields'].get('Full Name')) or cv(u['fields'].get('Email')) or '?')
            for u in cache.get_users()}


def _num(rec):
    n = cv(rec['fields'].get('№'))
    return n if isinstance(n, (int, float)) else 0


def chat_messages(chat_id):
    msgs = [m for m in cache.get_messages() if chat_id in lst(m['fields'].get('Чат'))]
    msgs.sort(key=lambda m: (m.get('createdTime', ''), _num(m)))
    return msgs


def participants(msgs):
    out = []
    for m in msgs:
        for uid in lst(m['fields'].get('Від')) + lst(m['fields'].get('До')):
            if uid not in out:
                out.append(uid)
    return out


def _local(created, full=False):
    d = parse_dt(created)
    return d.astimezone(TZ).strftime('%d.%m.%Y %H:%M' if full else '%d.%m %H:%M') if d else ''


def _chat(cid):
    return next((c for c in cache.get_chats() if c['id'] == cid), None)


def chat_parts(chat, msgs):
    """Учасники: у груповому чаті — список «Учасники», у звичайному — з повідомлень."""
    if chat and chat['fields'].get('Група'):
        return lst(chat['fields'].get('Учасники'))
    return participants(msgs)


def _can_manage(chat, me_id, role):
    """Перейменувати/видалити груповий чат може лише адмін або його автор."""
    if not chat or not chat['fields'].get('Група'):
        return True
    return role == 'admin' or me_id in lst(chat['fields'].get('Автор'))


def build_chats(role, me_id):
    names = user_names()
    by = {}
    for m in cache.get_messages():
        for cid in lst(m['fields'].get('Чат')):
            by.setdefault(cid, []).append(m)
    out = []
    for c in cache.get_chats():
        msgs = by.get(c['id']) or []
        group = bool(c['fields'].get('Група'))
        if not msgs and not group:
            continue
        msgs.sort(key=lambda m: (m.get('createdTime', ''), _num(m)))
        parts = chat_parts(c, msgs)
        member = me_id in parts
        if role != 'admin' and not member:
            continue
        who = [names.get(p, '?') for p in parts if p != me_id] if member else [names.get(p, '?') for p in parts]
        last = msgs[-1] if msgs else None
        out.append({'id': c['id'], 'title': str(c['fields'].get('Назва чату') or ''),
                    'with': ', '.join(who), 'member': member, 'group': group,
                    'manage': _can_manage(c, me_id, role),
                    'last_text': str(last['fields'].get('Текст') or '')[:60] if last else '',
                    'last_time': _local(last.get('createdTime')) if last else '',
                    'sort': last.get('createdTime', '') if last else (c.get('createdTime') or '')})
    out.sort(key=lambda x: x['sort'], reverse=True)
    return out


def _chat_access(chat_id, me_id, role):
    chat = _chat(chat_id)
    msgs = chat_messages(chat_id)
    parts = chat_parts(chat, msgs)
    if chat and (me_id in parts or role == 'admin'):
        return msgs, parts
    return None, None


@bp.route('/chats/<chat_id>/messages')
@login_only
def chat_msgs(chat_id):
    me = me_record(str(session['user']).strip().lower())
    msgs, parts = _chat_access(chat_id, me['id'] if me else '', session.get('role'))
    if msgs is None:
        return err('Доступ заборонено', 403)
    names = user_names()
    return jsonify(ok=True, member=bool(me and me['id'] in parts), messages=[{
        'id': m['id'], 'text': str(m['fields'].get('Текст') or ''),
        'from_name': names.get(cv(m['fields'].get('Від')), '?'),
        'mine': bool(me and cv(m['fields'].get('Від')) == me['id']),
        'can_edit': bool(me and (cv(m['fields'].get('Від')) == me['id'] or session.get('role') == 'admin')),
        'time': _local(m.get('createdTime'))} for m in msgs])


@bp.route('/chats/send', methods=['POST'])
@login_only
def chat_send():
    me = me_record(str(session['user']).strip().lower())
    cid = request.form.get('chat_id', '')
    text = request.form.get('text', '').strip()
    if not text:
        return err('Порожнє повідомлення')
    msgs, parts = _chat_access(cid, me['id'] if me else '', session.get('role'))
    if msgs is None or me['id'] not in parts:
        return err('Доступ заборонено', 403)
    others = [p for p in parts if p != me['id']]
    if not others:
        return err('Немає отримувача')
    fields = {'Текст': text, 'Від': [me['id']], 'Чат': [cid]}
    if not (_chat(cid)['fields'].get('Група')):
        fields['До'] = [others[0]]          # у груповому чаті отримувачі — усі учасники
    try:
        cache.create_record('messages', fields)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/chats/create', methods=['POST'])
@login_only
def chat_create():
    me = me_record(str(session['user']).strip().lower())
    role = session.get('role')
    text = request.form.get('text', '').strip()
    title = request.form.get('title', '').strip()
    users = {u['id']: u for u in cache.get_users()}
    tos = list(dict.fromkeys(t.strip() for t in request.form.getlist('to') if t.strip() and me and t.strip() != me['id']))
    if not me or not tos or any(t not in users for t in tos):
        return err('Оберіть співрозмовника')
    group = len(tos) > 1
    if group and role not in ('teacher', 'admin'):
        return err('Групи можуть створювати лише вчителі й адмін', 403)
    if role == 'student' and str(cv(users[tos[0]]['fields'].get('Role')) or '').lower() not in ('teacher', 'admin'):
        return err('Учень може писати лише вчителям і адміну', 403)
    if not text:
        return err('Введіть перше повідомлення')
    if len(title) > 80:
        return err('Назва задовга (до 80 символів)')
    names = user_names()
    chat = None
    try:
        if group:
            dflt = 'Група: ' + ', '.join(names.get(t, '?') for t in tos)
            chat = cache.create_record('chats', {'Назва чату': title or (dflt[:77] + '…' if len(dflt) > 80 else dflt),
                                                 'Група': True, 'Автор': [me['id']], 'Учасники': [me['id']] + tos})
            cache.create_record('messages', {'Текст': text, 'Від': [me['id']], 'Чат': [chat['id']]})
        else:
            chat = cache.create_record('chats', {'Назва чату': title or f"{names.get(me['id'])} — {names.get(tos[0])}"})
            cache.create_record('messages', {'Текст': text, 'Від': [me['id']], 'До': [tos[0]], 'Чат': [chat['id']]})
    except Exception as e:
        if chat:
            try:
                cache.delete_record('chats', chat['id'])
            except Exception:
                pass
        return err(str(e), 500)
    return jsonify(ok=True)


def _own_msg(mid):
    me = me_record(str(session['user']).strip().lower())
    rec = next((m for m in cache.get_messages() if m['id'] == mid), None)
    if not rec or not me:
        return None, err('Не знайдено', 404)
    if cv(rec['fields'].get('Від')) != me['id'] and session.get('role') != 'admin':
        return None, err('Доступ заборонено', 403)
    return rec, None


@bp.route('/chats/msg_edit', methods=['POST'])
@login_only
def msg_edit():
    rec, e = _own_msg(request.form.get('id', ''))
    text = request.form.get('text', '').strip()
    if e:
        return e
    if not text:
        return err('Порожнє повідомлення')
    try:
        cache.update_record('messages', rec['id'], {'Текст': text})
    except Exception as ex:
        return err(str(ex), 500)
    return jsonify(ok=True)


@bp.route('/chats/msg_delete', methods=['POST'])
@login_only
def msg_delete():
    rec, e = _own_msg(request.form.get('id', ''))
    if e:
        return e
    chats = lst(rec['fields'].get('Чат'))
    try:
        cache.delete_record('messages', rec['id'])
        for cid in chats:                      # порожній чат прибираємо
            ch = _chat(cid)
            if ch and not ch['fields'].get('Група') and not chat_messages(cid):
                cache.delete_record('chats', cid)   # порожній звичайний чат прибираємо (груповий лишається)
    except Exception as ex:
        return err(str(ex), 500)
    return jsonify(ok=True)


@bp.route('/chats/rename', methods=['POST'])
@login_only
def chat_rename():
    me = me_record(str(session['user']).strip().lower())
    cid = request.form.get('chat_id', '')
    title = request.form.get('title', '').strip()
    msgs, parts = _chat_access(cid, me['id'] if me else '', session.get('role'))
    if msgs is None or not _can_manage(_chat(cid), me['id'] if me else '', session.get('role')):
        return err('Доступ заборонено', 403)
    if not title:
        return err('Введіть назву')
    try:
        cache.update_record('chats', cid, {'Назва чату': title})
    except Exception as ex:
        return err(str(ex), 500)
    return jsonify(ok=True)


@bp.route('/chats/delete', methods=['POST'])
@login_only
def chat_delete():
    me = me_record(str(session['user']).strip().lower())
    cid = request.form.get('chat_id', '')
    msgs, parts = _chat_access(cid, me['id'] if me else '', session.get('role'))
    if msgs is None or not _can_manage(_chat(cid), me['id'] if me else '', session.get('role')):
        return err('Доступ заборонено', 403)
    try:
        cache.batch_delete('messages', [m['id'] for m in msgs])
        cache.delete_record('chats', cid)
    except Exception as ex:
        return err(str(ex), 500)
    return jsonify(ok=True)


# ---------- домашки ----------
MAX_MB = int(os.environ.get('MAX_UPLOAD_MB', '10'))  # ліміт одного файлу (безкоштовний Supabase — максимум 50 МБ)
MAX_ANSWER_FILES = int(os.environ.get('MAX_ANSWER_FILES', '5'))  # файлів-відповідей від одного учня на домашку
MAX_MEDIA_MB = int(os.environ.get('MAX_MEDIA_MB', '50'))  # ліміт одного фото/відео у вкладці «Файли»
MAX_BG_MB = int(os.environ.get('MAX_BG_MB', '5'))  # ліміт зображення-заставки
STORAGE_LIMIT_MB = int(os.environ.get('STORAGE_LIMIT_MB', '5120'))  # квота Storage (5 ГБ), для індикатора
TMP_DIR = os.path.join(tempfile.gettempdir(), 'school_uploads')
os.makedirs(TMP_DIR, exist_ok=True)
F_TASK, F_ANS = 'Завдання файл', 'Відповідь файли'


def _atts(f, key):
    return [{'id': a.get('id'), 'name': a.get('filename') or 'файл'} for a in (f.get(key) or [])]


def hw_assigned(f, st, email):
    if st and st['id'] in lst(f.get('Учні')):
        return True
    return email in [e.strip().lower() for e in lst(f.get('Email-и учнів'))]


def build_homework(role, st_rec, sname, email=''):
    pref = f'[{sname}] '
    out = []
    for r in cache.get_homework():
        f = r['fields']
        ids = lst(f.get('Учні'))
        if role == 'student' and not hw_assigned(f, st_rec, email):
            continue
        ans = _atts(f, F_ANS)
        if role == 'student':
            ans = [dict(a, name=a['name'][len(pref):]) for a in ans if a['name'].startswith(pref)]
        out.append({'id': r['id'], 'num': cv(f.get('№')) or 0, 'text': str(f.get('Завдання') or ''),
                    'student_ids': ids, 'student_names': lst(f.get('Імені учнів')),
                    'task_files': _atts(f, F_TASK), 'answer_files': ans})
    out.sort(key=lambda h: h['num'] if isinstance(h['num'], (int, float)) else 0, reverse=True)
    return out


def _save_files(key='files', limit_mb=None):
    """Зберігає завантажені файли у тимчасову теку (без читання в пам'ять)."""
    out = []
    try:
        for fl in request.files.getlist(key):
            if not fl or not fl.filename:
                continue
            name = os.path.basename(fl.filename.replace('\\', '/'))
            path = os.path.join(TMP_DIR, secrets.token_hex(16))
            fl.save(path)
            size = os.path.getsize(path)
            out.append((name, path, fl.mimetype or 'application/octet-stream', size))
            if size > (limit_mb or MAX_MB) * 1024 * 1024:
                raise ValueError(f'Файл «{name}» більший за {limit_mb or MAX_MB} МБ')
    except Exception:
        _cleanup(out)
        raise
    return out


def _cleanup(files):
    for f in files:
        try:
            os.remove(f[1])
        except OSError:
            pass


def _upload(rid, field, files, prefix=''):
    for name, path, ctype, size in files:
        with open(path, 'rb') as fh:
            cache.upload_attachment('homework', rid, field, prefix + name, fh, ctype)


@bp.route('/homework/save', methods=['POST'])
@login_only
@staff_only
def hw_save():
    f = request.form
    text = f.get('text', '').strip()
    students = [x for x in f.getlist('students') if x]
    if not students:
        return err('Оберіть учнів')
    try:
        files = _save_files()
    except ValueError as e:
        return err(str(e))
    if not text and not files and not f.get('id'):
        return err('Введіть завдання або додайте файл')
    try:
        hid = f.get('id', '').strip()
        if hid:
            fields = {'Завдання': text, 'Учні': students}
            rm = set(f.getlist('remove'))
            if rm:
                rec = cache.reload_record('homework', hid)
                fields[F_TASK] = [{'id': a['id']} for a in (rec['fields'].get(F_TASK) or []) if a['id'] not in rm]
            cache.update_record('homework', hid, fields)
        else:
            hid = cache.create_record('homework', {'Завдання': text, 'Учні': students})['id']
        _upload(hid, F_TASK, files)
    except Exception as e:
        return err(str(e), 500)
    finally:
        _cleanup(files)
    return jsonify(ok=True)


@bp.route('/homework/delete', methods=['POST'])
@login_only
@staff_only
def hw_delete():
    try:
        cache.delete_record('homework', request.form.get('id', ''))
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


def _student_ctx():
    email = str(session['user']).strip().lower()
    st = student_record(email)
    return st, student_name(email, st), email


@bp.route('/homework/answer', methods=['POST'])
@login_only
def hw_answer():
    if session.get('role') != 'student':
        return err('Доступ заборонено', 403)
    st, name, email = _student_ctx()
    hid = request.form.get('id', '')
    rec = next((r for r in cache.get_homework() if r['id'] == hid), None)
    if not rec or not hw_assigned(rec['fields'], st, email):
        return err('Не знайдено', 404)
    try:
        files = _save_files()
    except ValueError as e:
        return err(str(e))
    if not files:
        return err('Оберіть файли')
    mine = len([a for a in _atts(rec['fields'], F_ANS) if a['name'].startswith(f'[{name}] ')])
    if mine + len(files) > MAX_ANSWER_FILES:
        _cleanup(files)
        return err(f'Можна прикріпити максимум {MAX_ANSWER_FILES} файлів (уже є {mine}). Видаліть зайві.')
    try:
        _upload(hid, F_ANS, files, prefix=f'[{name}] ')
    except Exception as e:
        return err(str(e), 500)
    finally:
        _cleanup(files)
    return jsonify(ok=True)


@bp.route('/homework/file_delete', methods=['POST'])
@login_only
def hw_file_delete():
    role = session.get('role')
    hid, field, att = (request.form.get(k, '') for k in ('id', 'field', 'att'))
    key = F_TASK if field == 'task' else F_ANS
    try:
        rec = cache.reload_record('homework', hid)
    except Exception:
        return err('Не знайдено', 404)
    atts = rec['fields'].get(key) or []
    target = next((a for a in atts if a.get('id') == att), None)
    if not target:
        return err('Не знайдено', 404)
    if role == 'student':
        st, name, email = _student_ctx()
        if key != F_ANS or not hw_assigned(rec['fields'], st, email) \
                or not (target.get('filename') or '').startswith(f'[{name}] '):
            return err('Доступ заборонено', 403)
    try:
        cache.update_record('homework', hid, {key: [{'id': a['id']} for a in atts if a['id'] != att]})
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


# ---------- вкладка «Файли» (фото й відео) ----------
def _media_kind(name, mime):
    m = (mime or '').lower()
    ext = os.path.splitext(name)[1].lower()
    if m.startswith('image/') or ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.bmp'):
        return 'image'
    if m.startswith('video/') or ext in ('.mp4', '.mov', '.webm', '.m4v', '.avi', '.mkv'):
        return 'video'
    return None


def build_media(role, me_id):
    groups = {str(g['id']): {'id': str(g['id']), 'name': g['name'], 'dates': {}} for g in cache.get_media_groups()}
    for f in cache.get_media_files():
        g = groups.get(str(f['group_id']))
        if not g:
            continue
        d = str(f['taken_on'])
        g['dates'].setdefault(d, []).append({
            'id': str(f['id']), 'name': f['filename'], 'kind': f['kind'],
            'can_del': role == 'admin' or (role == 'teacher' and str(f.get('uploaded_by') or '') == me_id),
            '_t': f.get('created_at') or ''})
    out = []
    for g in groups.values():
        dates = []
        for d in sorted(g['dates'], reverse=True):           # нові дати — зверху
            fl = sorted(g['dates'][d], key=lambda x: x['_t'])
            for x in fl:
                x.pop('_t')
            dates.append({'date': d, 'label': '.'.join(reversed(d.split('-'))), 'files': fl})
        g['dates'] = dates
        g['latest'] = dates[0]['date'] if dates else ''
        out.append(g)
    out.sort(key=lambda g: (g['latest'], g['name'].lower()), reverse=True)
    return out


@bp.route('/media/file/<fid>')
@login_only
def media_get(fid):
    row = cache.media_file(fid)
    if not row:
        return 'Не знайдено', 404
    try:
        path = cache.local_file({'path': row['path']}, bucket=cache.MEDIA_BUCKET)
    except Exception:
        path = None
    if not path:
        return 'Файл недоступний у сховищі', 404
    resp = send_file(path, mimetype=row.get('mime') or 'application/octet-stream',
                     download_name=row['filename'], conditional=True)   # conditional => підтримка Range (перемотка відео)
    resp.headers['Cache-Control'] = 'private, max-age=3600'
    return resp


@bp.route('/media/group_create', methods=['POST'])
@login_only
@staff_only
def media_group_create():
    name = ' '.join(request.form.get('name', '').split())
    if not name:
        return err('Введіть назву групи')
    if len(name) > 80:
        return err('Назва групи задовга (до 80 символів)')
    try:
        g = cache.create_media_group(name)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, id=str(g['id']), name=g['name'])


@bp.route('/media/upload', methods=['POST'])
@login_only
@staff_only
def media_upload():
    gid, day = request.form.get('group_id', '').strip(), request.form.get('date', '').strip()
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except ValueError:
        return err('Вкажіть дату')
    if not gid:
        return err('Оберіть групу')
    try:
        files = _save_files(limit_mb=MAX_MEDIA_MB)
    except ValueError as e:
        return err(str(e))
    me = me_record(str(session['user']).strip().lower())
    try:
        if not files:
            return err('Оберіть файли')
        kinds = [_media_kind(n, c) for n, _, c, _ in files]
        if None in kinds:
            return err(f'«{files[kinds.index(None)][0]}» — не фото і не відео')
        for (name, path, ctype, size), kind in zip(files, kinds):
            with open(path, 'rb') as fh:
                cache.add_media(gid, day, name, fh, ctype, kind, me['id'] if me else None)
    except KeyError as e:
        return err(str(e).strip("'\""))
    except Exception as e:
        return err(str(e), 500)
    finally:
        _cleanup(files)
    return jsonify(ok=True)


@bp.route('/media/delete', methods=['POST'])
@login_only
@staff_only
def media_delete():
    row = cache.media_file(request.form.get('id', ''))
    if not row:
        return err('Не знайдено', 404)
    me = me_record(str(session['user']).strip().lower())
    if session.get('role') != 'admin' and str(row.get('uploaded_by') or '') != (me['id'] if me else ''):
        return err('Видаляти можна лише свої файли', 403)
    try:
        cache.delete_media(row['id'])
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/media/group_delete', methods=['POST'])
@login_only
@staff_only
def media_group_delete():
    if session.get('role') != 'admin':
        return err('Групу може видалити лише адмін', 403)
    try:
        cache.delete_media_group(request.form.get('id', ''))
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


def admin_only(fn):
    @wraps(fn)
    def w(*a, **k):
        if session.get('role') != 'admin':
            return err('Доступ заборонено', 403)
        return fn(*a, **k)
    return w


# ---------- Профіль: заставка ----------
_BG_EXT = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
           '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}   # SVG свідомо заборонено
_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def _bg_info(me_id):
    r = cache.get_background(me_id) if me_id else None
    if not r:
        return {'color': '', 'has_file': False, 'filename': '', 'mb': 0, 'date': '', 'css': ''}
    ts = str(r.get('updated_at') or '')
    v = re.sub(r'\D', '', ts)[:14] or '0'
    if r.get('file_path'):
        css = "body{background:#222 url('/profile/bg?v=%s') center/cover fixed no-repeat !important}" % v
    else:
        css = 'body{background:%s !important}' % r['color'] if _HEX.match(r.get('color') or '') else ''
    return {'color': r.get('color') or '', 'has_file': bool(r.get('file_path')), 'filename': r.get('filename') or '',
            'mb': round(int(r.get('size') or 0) / 1048576, 2), 'date': str(r.get('uploaded_at') or '')[:10], 'css': css}


def _me_id():
    me = me_record(str(session['user']).strip().lower())
    return me['id'] if me else None


@bp.route('/profile/bg')
@login_only
def profile_bg():
    r = cache.get_background(_me_id())
    if not r or not r.get('file_path'):
        return 'Не знайдено', 404
    try:
        path = cache.local_file({'path': r['file_path']}, bucket=cache.BG_BUCKET)
    except Exception:
        path = None
    if not path:
        return 'Файл недоступний', 404
    resp = send_file(path, mimetype=r.get('mime') or 'image/jpeg', conditional=True)
    resp.headers['Cache-Control'] = 'private, max-age=86400'     # URL має версію (?v=), тож оновлення підхопиться
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@bp.route('/profile/color', methods=['POST'])
@login_only
def profile_color():
    color = request.form.get('color', '').strip()
    uid = _me_id()
    if not uid:
        return err('Користувача не знайдено', 404)
    if not _HEX.match(color):
        return err('Невірний колір (потрібен код виду #RRGGBB)')
    try:
        cache.set_bg_color(uid, color)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/profile/upload', methods=['POST'])
@login_only
def profile_upload():
    uid = _me_id()
    if not uid:
        return err('Користувача не знайдено', 404)
    try:
        files = _save_files('bg', limit_mb=MAX_BG_MB)
    except ValueError as e:
        return err(str(e))
    try:
        if not files:
            return err('Оберіть зображення')
        name = files[0][0]
        mime = _BG_EXT.get(os.path.splitext(name)[1].lower())
        if not mime:
            return err('Дозволені лише зображення: JPG, PNG, GIF, WEBP, BMP')
        with open(files[0][1], 'rb') as fh:
            cache.set_bg_file(uid, name, fh, mime)
    except Exception as e:
        return err(str(e), 500)
    finally:
        _cleanup(files)
    return jsonify(ok=True)


@bp.route('/profile/reset', methods=['POST'])
@login_only
def profile_reset():
    uid = _me_id()
    try:
        if uid:
            cache.clear_bg(uid)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


def build_bg_manager():
    un, rl = user_names(), {u['id']: str(cv(u['fields'].get('Role')) or '').lower() for u in cache.get_users()}
    rows = []
    for r in cache.bg_report():
        rows.append({'uid': r['user_id'], 'name': un.get(r['user_id'], '?'), 'role': ROLE_LABEL.get(rl.get(r['user_id'], ''), ''),
                     'color': r.get('color') or '', 'file': r.get('filename') or '', 'size': int(r.get('size') or 0),
                     'mb': round(int(r.get('size') or 0) / 1048576, 2),
                     'date': str(r.get('uploaded_at') or '')[:10] or '—', 'upd': str(r.get('updated_at') or '')[:10]})
    rows.sort(key=lambda x: x['size'], reverse=True)
    return {'files': rows}


@bp.route('/backgrounds/delete', methods=['POST'])
@login_only
@admin_only
def bg_delete():
    try:
        n = cache.delete_bg_many(request.form.getlist('ids'))
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, deleted=n)


@bp.route('/backgrounds/old', methods=['POST'])
@login_only
@admin_only
def bg_old():
    try:
        days = max(1, int(request.form.get('days', '0')))
    except ValueError:
        return err('Вкажіть кількість днів')
    cut = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    ids = [r['user_id'] for r in cache.bg_report()
           if r.get('file_path') and cache._ts(r.get('uploaded_at')) and cache._ts(r.get('uploaded_at')) < cut]
    try:
        n = cache.delete_bg_many(ids)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, deleted=n)


@bp.route('/backgrounds/orphans', methods=['POST'])
@login_only
@admin_only
def bg_orphans():
    try:
        cache.clean_orphans(cache.BG_BUCKET)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


def build_file_manager():
    rows = cache.file_report()
    nums = {r['id']: cv(r['fields'].get('№')) for r in cache.get_homework()}
    for r in rows:
        r['num'] = nums.get(r['hw'], '?')
        r['kind'] = 'task' if r['field'] == F_TASK else 'answer'
        r['key'] = f"{r['hw']}|{r['field']}|{r['id']}"
        r['mb'] = round(r['size'] / 1048576, 2)
        r['date'] = (r['uploaded'] or '')[:10] or '—'
    rows.sort(key=lambda r: r['size'], reverse=True)
    hw_mb = sum(r['size'] for r in rows) / 1048576
    media_mb = cache.media_bytes() / 1048576
    bg_mb = cache.bg_bytes() / 1048576
    used = hw_mb + media_mb + bg_mb  # квота Storage спільна: домашки + фото/відео + заставки
    return {'files': rows, 'used_mb': round(used, 1), 'hw_mb': round(hw_mb, 1), 'media_mb': round(media_mb, 1), 'bg_mb': round(bg_mb, 1), 'limit_mb': STORAGE_LIMIT_MB,
            'pct': min(100, round(used / STORAGE_LIMIT_MB * 100)) if STORAGE_LIMIT_MB else 0}


def build_media_manager():
    gn = {str(g['id']): g['name'] for g in cache.get_media_groups()}
    un = user_names()
    rows = []
    for f in cache.get_media_files():
        d = str(f['taken_on'])
        rows.append({'id': str(f['id']), 'name': f['filename'], 'group': gn.get(str(f['group_id']), '?'),
                     'kind': f['kind'], 'mb': round(int(f.get('size') or 0) / 1048576, 2), 'size': int(f.get('size') or 0),
                     'taken': '.'.join(reversed(d.split('-'))),
                     'by': un.get(str(f.get('uploaded_by') or ''), '—'),
                     'date': str(f.get('created_at') or '')[:10] or '—'})
    rows.sort(key=lambda r: r['size'], reverse=True)
    return {'files': rows}


@bp.route('/media/files_delete', methods=['POST'])
@login_only
@admin_only
def media_files_delete():
    try:
        n = cache.delete_media_many(request.form.getlist('ids'))
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, deleted=n)


@bp.route('/media/files_old', methods=['POST'])
@login_only
@admin_only
def media_files_old():
    try:
        days = max(1, int(request.form.get('days', '0')))
    except ValueError:
        return err('Вкажіть кількість днів')
    cut = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    ids = [str(f['id']) for f in cache.get_media_files() if cache._ts(f.get('created_at')) and cache._ts(f.get('created_at')) < cut]
    try:
        n = cache.delete_media_many(ids)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, deleted=n)


@bp.route('/media/orphans', methods=['POST'])
@login_only
@admin_only
def media_orphans():
    try:
        cache.clean_orphans(cache.MEDIA_BUCKET)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/homework/files_delete', methods=['POST'])
@login_only
@admin_only
def hw_files_delete():
    items = [tuple(x.split('|', 2)) for x in request.form.getlist('items') if x.count('|') == 2]
    try:
        n = cache.delete_files(items)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, deleted=n)


@bp.route('/homework/files_old', methods=['POST'])
@login_only
@admin_only
def hw_files_old():
    try:
        days = max(1, int(request.form.get('days', '0')))
    except ValueError:
        return err('Вкажіть кількість днів')
    cut = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    items = [(r['hw'], r['field'], r['id']) for r in cache.file_report() if r['uploaded'] and r['uploaded'] < cut]
    try:
        n = cache.delete_files(items)
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True, deleted=n)


@bp.route('/homework/orphans', methods=['POST'])
@login_only
@admin_only
def hw_orphans():
    try:
        cache.clean_orphans()
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/homework/file/<rid>/<field>/<att>')
@login_only
def hw_file(rid, field, att):
    role = session.get('role')
    key = F_TASK if field == 'task' else F_ANS
    rec = next((r for r in cache.get_homework() if r['id'] == rid), None)
    if not rec:
        return 'Не знайдено', 404
    target = next((a for a in (rec['fields'].get(key) or []) if a.get('id') == att), None)
    if not target:
        return 'Не знайдено', 404
    if role == 'student':
        st, name, email = _student_ctx()
        if not hw_assigned(rec['fields'], st, email):
            return 'Доступ заборонено', 403
        if key == F_ANS and not (target.get('filename') or '').startswith(f'[{name}] '):
            return 'Доступ заборонено', 403
    try:
        path = cache.local_file(target)
    except Exception:
        path = None
    if not path:
        return 'Файл недоступний у сховищі — завантажте його заново', 404
    resp = send_file(path, mimetype=target.get('type') or 'application/octet-stream',
                     download_name=target.get('filename') or 'файл', conditional=True)
    resp.headers['Cache-Control'] = 'private, max-age=3600'   # браузер теж кешує на годину
    return resp


# ---------- контекст для шаблонів ----------

def portal_context(role, email):
    email = str(email or '').strip().lower()
    me = me_record(email)
    me_id = me['id'] if me else ''
    st_rec = student_record(email) if role == 'student' else None
    staff = role != 'student'
    students = sorted(
        ({'id': s['id'], 'name': str(cv(s['fields'].get("Ім'я учня")) or ''),
          'cls': str(cv(s['fields'].get('Клас')) or '')}
         for s in cache.get_students() if cv(s['fields'].get("Ім'я учня"))),
        key=lambda x: x['name'])
    users = []
    if staff:
        users = sorted(
            ({'id': u['id'], 'name': str(cv(u['fields'].get('Full Name')) or cv(u['fields'].get('Email')) or ''),
              'role': str(cv(u['fields'].get('Role')) or '').lower()}
             for u in cache.get_users()),
            key=lambda x: x['name'])
        for u in users:
            u['label'] = ROLE_LABEL.get(u['role'], u['role'])
    # вкладки «Оцінки» і «Статистика» бачать вчителі/адмін та учні з галочкою «Оцінюється»
    show_grades = staff or bool(st_rec and st_rec['fields'].get('Оцінюється'))
    graded_names = sorted(str(cv(s['fields'].get("Ім'я учня")) or '') for s in cache.get_students()
                          if s['fields'].get('Оцінюється') and cv(s['fields'].get("Ім'я учня")))
    return {
        'portal_role': role,
        'me_id': me_id,
        'show_grades': show_grades,
        'stats_rows': build_stats(role, email, st_rec) if show_grades else [],
        'stats_students': graded_names if staff else [],
        'stats_subjects': staff_subject_names(role, email) if staff else [],
        'discipline_list': build_discipline(role, st_rec),
        'portal_students': students if staff else [],
        'portal_users': users,
        'announcements': build_announcements(role, me_id),
        'chat_list': build_chats(role, me_id),
        'chat_users': [
            {'id': u['id'], 'name': str(cv(u['fields'].get('Full Name')) or cv(u['fields'].get('Email')) or ''),
             'label': ROLE_LABEL.get(str(cv(u['fields'].get('Role')) or '').lower(), ''),
             'role': str(cv(u['fields'].get('Role')) or '').lower()}
            for u in cache.get_users()
            if u['id'] != me_id and (staff or str(cv(u['fields'].get('Role')) or '').lower() in ('teacher', 'admin'))],
        'homework_list': build_homework(role, st_rec, student_name(email, st_rec), email),
        'max_upload_mb': MAX_MB,
        'max_media_mb': MAX_MEDIA_MB,
        'media_groups': build_media(role, me_id),
        'file_mgr': build_file_manager() if role == 'admin' else None,
        'media_mgr': build_media_manager() if role == 'admin' else None,
        'bg_mgr': build_bg_manager() if role == 'admin' else None,
        'profile_bg': _bg_info(me_id),
        'bg_css': _bg_info(me_id)['css'],
        'max_bg_mb': MAX_BG_MB,
    }