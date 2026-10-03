"""Вкладки кабінетів: Статистика, Догани, Оголошення (етап 2)."""
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import os

from flask import Blueprint, request, session, jsonify, redirect

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
    for s in cache.get_students():
        if _em(s['fields']) == email:
            return s


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
    for rec in cache.get_grades():
        f = rec['fields']
        st = str(cv(f.get("Ім'я учня")) or '')
        if role == 'student' and not (_em(f, 'Email учня') == email or (name and st == name)):
            continue
        subj = str(cv(f.get('Назва предмета')) or cv(f.get('Предмет')) or '')
        d = str(cv(f.get('Дата виставлення оцінки')) or cv(f.get('Дата')) or '')
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


def _local(created):
    d = parse_dt(created)
    return d.astimezone(TZ).strftime('%d.%m %H:%M') if d else ''


def build_chats(role, me_id):
    names = user_names()
    by = {}
    for m in cache.get_messages():
        for cid in lst(m['fields'].get('Чат')):
            by.setdefault(cid, []).append(m)
    out = []
    for c in cache.get_chats():
        msgs = by.get(c['id'])
        if not msgs:
            continue
        msgs.sort(key=lambda m: (m.get('createdTime', ''), _num(m)))
        parts = participants(msgs)
        member = me_id in parts
        if role != 'admin' and not member:
            continue
        who = [names.get(p, '?') for p in parts if p != me_id] if member else [names.get(p, '?') for p in parts]
        last = msgs[-1]
        out.append({'id': c['id'], 'title': str(c['fields'].get('Назва чату') or ''),
                    'with': ', '.join(who), 'member': member,
                    'last_text': str(last['fields'].get('Текст') or '')[:60],
                    'last_time': _local(last.get('createdTime')),
                    'sort': last.get('createdTime', '')})
    out.sort(key=lambda x: x['sort'], reverse=True)
    return out


def _chat_access(chat_id, me_id, role):
    msgs = chat_messages(chat_id)
    parts = participants(msgs)
    if me_id in parts or role == 'admin':
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
    try:
        cache.create_record('messages', {'Текст': text, 'Від': [me['id']], 'До': [others[0]], 'Чат': [cid]})
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/chats/create', methods=['POST'])
@login_only
def chat_create():
    me = me_record(str(session['user']).strip().lower())
    to = request.form.get('to', '').strip()
    text = request.form.get('text', '').strip()
    title = request.form.get('title', '').strip()
    target = next((u for u in cache.get_users() if u['id'] == to), None)
    if not me or not target or to == me['id']:
        return err('Оберіть співрозмовника')
    if session.get('role') == 'student' and str(cv(target['fields'].get('Role')) or '').lower() not in ('teacher', 'admin'):
        return err('Учень може писати лише вчителям і адміну', 403)
    if not text:
        return err('Введіть перше повідомлення')
    names = user_names()
    chat = None
    try:
        chat = cache.create_record('chats', {'Назва чату': title or f"{names.get(me['id'])} — {names.get(to)}"})
        cache.create_record('messages', {'Текст': text, 'Від': [me['id']], 'До': [to], 'Чат': [chat['id']]})
    except Exception as e:
        if chat:
            try:
                cache.delete_record('chats', chat['id'])
            except Exception:
                pass
        return err(str(e), 500)
    return jsonify(ok=True)


# ---------- домашки ----------
MAX_FILE = 5 * 1024 * 1024
F_TASK, F_ANS = 'Завдання файл', 'Відповідь файли'


def _atts(f, key):
    return [{'id': a.get('id'), 'name': a.get('filename') or 'файл'} for a in (f.get(key) or [])]


def build_homework(role, st_rec, sname):
    pref = f'[{sname}] '
    out = []
    for r in cache.get_homework():
        f = r['fields']
        ids = lst(f.get('Учні'))
        if role == 'student' and not (st_rec and st_rec['id'] in ids):
            continue
        ans = _atts(f, F_ANS)
        if role == 'student':
            ans = [dict(a, name=a['name'][len(pref):]) for a in ans if a['name'].startswith(pref)]
        out.append({'id': r['id'], 'num': cv(f.get('№')) or 0, 'text': str(f.get('Завдання') or ''),
                    'student_ids': ids, 'student_names': lst(f.get('Імені учнів')),
                    'task_files': _atts(f, F_TASK), 'answer_files': ans})
    out.sort(key=lambda h: h['num'] if isinstance(h['num'], (int, float)) else 0, reverse=True)
    return out


def _read_files(key='files'):
    files = [fl for fl in request.files.getlist(key) if fl and fl.filename]
    data = []
    for fl in files:
        c = fl.read()
        if len(c) > MAX_FILE:
            raise ValueError(f'Файл «{fl.filename}» більший за 5 МБ')
        data.append((os.path.basename(fl.filename.replace('\\', '/')), c, fl.mimetype or 'application/octet-stream'))
    return data


def _upload(rid, field, files, prefix=''):
    for name, content, ctype in files:
        cache.upload_attachment('homework', rid, field, prefix + name, content, ctype)


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
        files = _read_files()
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
    st = student_record(str(session['user']).strip().lower())
    name = str(cv(st['fields'].get("Ім'я учня")) or '') if st else ''
    return st, name


@bp.route('/homework/answer', methods=['POST'])
@login_only
def hw_answer():
    if session.get('role') != 'student':
        return err('Доступ заборонено', 403)
    st, name = _student_ctx()
    hid = request.form.get('id', '')
    rec = next((r for r in cache.get_homework() if r['id'] == hid), None)
    if not rec or not st or st['id'] not in lst(rec['fields'].get('Учні')):
        return err('Не знайдено', 404)
    try:
        files = _read_files()
    except ValueError as e:
        return err(str(e))
    if not files:
        return err('Оберіть файли')
    try:
        _upload(hid, F_ANS, files, prefix=f'[{name}] ')
    except Exception as e:
        return err(str(e), 500)
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
        st, name = _student_ctx()
        if key != F_ANS or not st or st['id'] not in lst(rec['fields'].get('Учні')) \
                or not (target.get('filename') or '').startswith(f'[{name}] '):
            return err('Доступ заборонено', 403)
    try:
        cache.update_record('homework', hid, {key: [{'id': a['id']} for a in atts if a['id'] != att]})
    except Exception as e:
        return err(str(e), 500)
    return jsonify(ok=True)


@bp.route('/homework/file/<rid>/<field>/<att>')
@login_only
def hw_file(rid, field, att):
    role = session.get('role')
    key = F_TASK if field == 'task' else F_ANS
    try:
        rec = cache.reload_record('homework', rid)   # свіжі URL вкладень
    except Exception:
        return 'Не знайдено', 404
    target = next((a for a in (rec['fields'].get(key) or []) if a.get('id') == att), None)
    if not target:
        return 'Не знайдено', 404
    if role == 'student':
        st, name = _student_ctx()
        if not st or st['id'] not in lst(rec['fields'].get('Учні')):
            return 'Доступ заборонено', 403
        if key == F_ANS and not (target.get('filename') or '').startswith(f'[{name}] '):
            return 'Доступ заборонено', 403
    return redirect(target['url'])


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
    return {
        'portal_role': role,
        'me_id': me_id,
        'stats_rows': build_stats(role, email, st_rec),
        'stats_students': [s['name'] for s in students] if staff else [],
        'stats_subjects': staff_subject_names(role, email) if staff else [],
        'discipline_list': build_discipline(role, st_rec),
        'portal_students': students if staff else [],
        'portal_users': users,
        'announcements': build_announcements(role, me_id),
        'chat_list': build_chats(role, me_id),
        'chat_users': [
            {'id': u['id'], 'name': str(cv(u['fields'].get('Full Name')) or cv(u['fields'].get('Email')) or ''),
             'label': ROLE_LABEL.get(str(cv(u['fields'].get('Role')) or '').lower(), '')}
            for u in cache.get_users()
            if u['id'] != me_id and (staff or str(cv(u['fields'].get('Role')) or '').lower() in ('teacher', 'admin'))],
        'homework_list': build_homework(role, st_rec,
                                        str(cv(st_rec['fields'].get("Ім'я учня")) or '') if st_rec else ''),
    }
