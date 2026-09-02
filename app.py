import os
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from pyairtable import Api
from datetime import date
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default_secret_key_for_dev')

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')

mail = Mail(app)
serializer = URLSafeTimedSerializer(app.secret_key)

# Отримуємо ключі з змінних оточення Render
AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID')

# Підключаємося до Airtable
api = Api(AIRTABLE_API_KEY)
grades_table = api.table(AIRTABLE_BASE_ID, 'Оцінки')
users_table = api.table(AIRTABLE_BASE_ID, 'Users')
subjects_table = api.table(AIRTABLE_BASE_ID, 'Предмети')
students_table = api.table(AIRTABLE_BASE_ID, 'Учні')

def clean_value(val):
    """Якщо значення прийшло як список ['...'], витягуємо перший елемент"""
    if isinstance(val, list) and len(val) > 0:
        return val[0]
    return val if val is not None else ''

@app.route('/')
def home():
    if 'user' in session:
        if session['role'] == 'teacher':
            return redirect(url_for('teacher_dashboard'))
        elif session['role'] == 'admin':
            return redirect(url_for('admin_page'))
        else:
            return redirect(url_for('student_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Перевірка: якщо користувач ВЖЕ авторизований, редиректимо його геть з логину
    if 'user' in session:
        role = session.get('role')
        if role == 'admin':
            return redirect(url_for('admin_page'))
        elif role == 'teacher':
            return redirect(url_for('teacher_dashboard'))
        else:
            return redirect(url_for('student_dashboard'))

    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        
        # Шукаємо користувача в таблиці Users за введеним Email
        records = users_table.all(formula=f"{{Email}} = '{email}'")
        
        if records:
            user_fields = records[0]['fields']
            stored_password = clean_value(user_fields.get('Password'))
            user_role = clean_value(user_fields.get('Role'))
            
            # Перевіряємо, чи збігається пароль
            if str(stored_password) == password:
                session['user'] = email
                role_lower = str(user_role).lower()
                if role_lower == 'admin':
                    session['role'] = 'admin'
                    return redirect(url_for('admin_page'))
                elif role_lower == 'teacher':
                    session['role'] = 'teacher'
                    return redirect(url_for('teacher_dashboard'))
                else:
                    session['role'] = 'student'
                    return redirect(url_for('student_dashboard'))
            else:
                error = 'Неправильний пароль'
        else:
            error = 'Користувача з таким Email не знайдено'
            
    return render_template('login.html', error=error)

@app.route('/admin')
def admin_page():
    # Перевірка авторизації та ролі
    if 'user' not in session:
        return redirect(url_for('login'))
    
    if session.get('role') != 'admin':
        return "Доступ заборонено. Потрібні права адміністратора.", 403

    admin_email = session['user']

    users = users_table.all()
    students = students_table.all()
    subjects = subjects_table.all()
    grades = grades_table.all()

    # 1. Отримуємо ВСІ предмети для адміна (не за фільтром вчителя)
    all_subjects_records = subjects_table.all()
    all_subjects = []
    for subj in all_subjects_records:
        f = subj['fields']
        s_name = clean_value(f.get('Назва предмета'))
        if s_name:
            all_subjects.append({
                'id': subj['id'],
                'name': s_name
            })

    # Сортуємо предмети за алфавітом
    all_subjects = sorted(all_subjects, key=lambda x: x['name'])

    # Отримуємо вибраний предмет (за замовчуванням — перший зі списку)
    selected_subject_id = request.args.get('subject_id')
    if not selected_subject_id and all_subjects:
        selected_subject_id = all_subjects[0]['id']

    selected_subject_name = None
    for s in all_subjects:
        if s['id'] == selected_subject_id:
            selected_subject_name = s['name']
            break

    # 2. Отримуємо оцінки для вибраного предмета
    all_grades = grades_table.all()
    students_set = set()
    dates_set = set()
    raw_grades = []

    for rec in all_grades:
        f = rec['fields']
        subj_name = clean_value(f.get('Назва предмета')) or clean_value(f.get('Предмет'))
        
        if selected_subject_name and str(subj_name).strip() == str(selected_subject_name).strip():
            student = clean_value(f.get("Ім'я учня"))
            dt_val = clean_value(f.get('Дата виставлення оцінки')) or clean_value(f.get('Дата')) or 'Без дати'
            grade = clean_value(f.get('Оцінка'))
            status = clean_value(f.get('Статус'))
            comment = clean_value(f.get('Коментар вчителя'))

            if student:
                students_set.add(str(student))
                dates_set.add(str(dt_val))
                raw_grades.append({
                    'id': rec['id'],
                    'student': str(student),
                    'date': str(dt_val),
                    'grade': grade,
                    'status': status or 'Присутній',
                    'comment': comment or ''
                })

    students_matrix_list = sorted(list(students_set))
    dates_matrix_list = sorted(list(dates_set))

    matrix = {st: {dt: [] for dt in dates_matrix_list} for st in students_matrix_list}
    for g in raw_grades:
        matrix[g['student']][g['date']].append(g)

    # 3. Список всіх учнів для форми виставлення
    student_records = students_table.all()
    students_list = []
    for st in student_records:
        st_id = st['id']
        st_name = clean_value(st['fields'].get("Ім'я учня"))
        st_class = clean_value(st['fields'].get("Клас"))
        if st_name:
            students_list.append((st_id, st_name, st_class))

    students_list = sorted(students_list, key=lambda x: x[1])

    # 4. Список усіх вчителів та їх закріплених предметів (для модального вікна / блоку призначень)
    users_records = users_table.all()
    teachers_list = []
    for u in users_records:
        uf = u['fields']
        u_role = str(clean_value(uf.get('Role'))).lower()
        if u_role == 'teacher':
            t_id = u['id']
            t_name = clean_value(uf.get('Full Name')) or clean_value(uf.get('Email'))
            t_email = clean_value(uf.get('Email'))
            # Отримуємо ID предметів, перетворюючи всі елементи на рядки
            t_subjs = uf.get('Предмети', [])
            if not isinstance(t_subjs, list):
                t_subjs = [t_subjs] if t_subjs else []
            t_subjs = [str(sid) for sid in t_subjs if sid]

            teachers_list.append({
                'id': t_id,
                'name': t_name,
                'email': t_email,
                'assigned_subject_ids': t_subjs
            })

    today_str = date.today().isoformat()

    return render_template(
        'admin.html',
        matrix=matrix,
        matrix_students=students_matrix_list,
        matrix_dates=dates_matrix_list,
        all_subjects=all_subjects,
        selected_subject_id=selected_subject_id,
        selected_subject_name=selected_subject_name,
        students=students_list,
        teachers=teachers_list,
        today_date=today_str,
        email=admin_email,
        users=users,
        grades=grades
    )

@app.route('/admin/assign_subject', methods=['POST'])
def assign_subject():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': 'Недостатньо прав'}), 403

    try:
        teacher_id = request.form.get('teacher_id')
        subject_ids = request.form.getlist('subject_ids[]')

        if not teacher_id:
            return jsonify({'status': 'error', 'message': 'Вчителя не вказано'}), 400

        # Оновлюємо зв'язок Link to Record полів у таблиці Users
        users_table.update(teacher_id, {'Предмети': subject_ids})
        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500



@app.route('/student')
def student_dashboard():
    if session.get('role') != 'student':
        return redirect(url_for('login'))
    
    student_email = str(session.get('user', '')).strip().lower()

    # 1. Знаходимо ім'я учня за його Email з таблиці "Users" або "Учні"
    user_records = users_table.all(formula=f"LOWER({{Email}}) = '{student_email}'")
    student_name = None
    if user_records:
        student_name = clean_value(user_records[0]['fields'].get('Full Name'))

    # 2. Отримуємо всі оцінки для цього учня
    all_grades = grades_table.all()
    subjects_set = set()
    dates_set = set()
    raw_grades = []

    for rec in all_grades:
        f = rec['fields']
        st_name = clean_value(f.get("Ім'я учня"))
        
        # Перевіряємо за іменем учня або за Email учня
        rec_email = str(clean_value(f.get('Email учня')) or '').strip().lower()
        if (student_name and str(st_name).strip() == str(student_name).strip()) or rec_email == student_email:
            subj_name = clean_value(f.get('Назва предмета')) or clean_value(f.get('Предмет'))
            dt_val = clean_value(f.get('Дата виставлення оцінки')) or clean_value(f.get('Дата')) or 'Без дати'
            grade = clean_value(f.get('Оцінка'))
            status = clean_value(f.get('Статус'))
            comment = clean_value(f.get('Коментар вчителя'))

            if subj_name:
                subjects_set.add(str(subj_name))
                dates_set.add(str(dt_val))
                raw_grades.append({
                    'subject': str(subj_name),
                    'date': str(dt_val),
                    'grade': grade,
                    'status': status or 'Присутній',
                    'comment': comment or ''
                })

    subjects_list = sorted(list(subjects_set))
    dates_list = sorted(list(dates_set))

    # Формуємо матрицю: matrix[subject][date] = grade_object
    matrix = {sb: {dt: None for dt in dates_list} for sb in subjects_list}
    for g in raw_grades:
        matrix[g['subject']][g['date']] = g

    return render_template(
        'student.html',
        matrix=matrix,
        subjects=subjects_list,
        dates=dates_list,
        email=student_email
    )



@app.route('/teacher')
def teacher_dashboard():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    teacher_email = str(session.get('user', '')).strip().lower()

    # 1. Знаходимо всі предмети, які викладає ЦЕЙ вчитель (фільтр за Email у таблиці "Предмети")
    all_subjects = subjects_table.all()
    teacher_subjects = []
    
    for subj in all_subjects:
        f = subj['fields']
        subj_email = str(clean_value(f.get('Email')) or '').strip().lower()
        if subj_email == teacher_email:
            teacher_subjects.append({
                'id': subj['id'],
                'name': clean_value(f.get('Назва предмета'))
            })

    # Отримуємо вибраний предмет зі списку (або перший за замовчуванням)
    selected_subject_id = request.args.get('subject_id')
    if not selected_subject_id and teacher_subjects:
        selected_subject_id = teacher_subjects[0]['id']

    selected_subject_name = None
    for s in teacher_subjects:
        if s['id'] == selected_subject_id:
            selected_subject_name = s['name']
            break

    # 2. Отримуємо оцінки ЛИШЕ для вибраного предмета цього вчителя
    all_grades = grades_table.all()
    students_set = set()
    dates_set = set()
    raw_grades = []

    for rec in all_grades:
        f = rec['fields']
        
        # Беремо текстову назву з Lookup-поля "Назва предмета"
        subj_name = clean_value(f.get('Назва предмета')) or clean_value(f.get('Предмет'))
        
        # Якщо предмет збігається з вибраним предметом вчителя
        if selected_subject_name and str(subj_name).strip() == str(selected_subject_name).strip():
            # Беремо ім'я учня з Lookup-поля "Ім'я учня"
            student = clean_value(f.get("Ім'я учня"))
            dt_val = clean_value(f.get('Дата виставлення оцінки')) or clean_value(f.get('Дата')) or 'Без дати'
            grade = clean_value(f.get('Оцінка'))
            status = clean_value(f.get('Статус'))
            comment = clean_value(f.get('Коментар вчителя'))

            if student:
                students_set.add(str(student))
                dates_set.add(str(dt_val))
                raw_grades.append({
                    'id': rec['id'],
                    'student': str(student),
                    'date': str(dt_val),
                    'grade': grade,
                    'status': status or 'Присутній',
                    'comment': comment or ''
                })

    students_matrix_list = sorted(list(students_set))
    dates_matrix_list = sorted(list(dates_set))

    # Створюємо матрицю журнала: matrix[student][date] = [список об'єктів оцінок]
    matrix = {st: {dt: [] for dt in dates_matrix_list} for st in students_matrix_list}
    for g in raw_grades:
        matrix[g['student']][g['date']].append(g)

    # 3. Список всіх учнів для форми виставлення
    student_records = students_table.all()
    students_list = []
    for st in student_records:
        st_id = st['id']
        st_name = clean_value(st['fields'].get("Ім'я учня"))
        st_class = clean_value(st['fields'].get("Клас"))
        if st_name:
            students_list.append((st_id, st_name, st_class))

    today_str = date.today().isoformat()

    return render_template(
        'teacher.html',
        matrix=matrix,
        matrix_students=students_matrix_list,
        matrix_dates=dates_matrix_list,
        teacher_subjects=teacher_subjects,
        selected_subject_id=selected_subject_id,
        selected_subject_name=selected_subject_name,
        students=students_list,
        today_date=today_str,
        email=teacher_email
    )



@app.route('/admin/create_user', methods=['POST'])
def admin_create_user():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    data = request.json
    full_name = data.get('full_name')
    email = data.get('email')
    role = data.get('role')  # 'admin', 'teacher', або 'child'
    password = data.get('password')
    class_name = data.get('class_name')  # Якщо створюємо учня

    if not full_name or not email or not role or not password:
        return jsonify({'status': 'error', 'message': 'Усі обов’язкові поля мають бути заповнені'}), 400

    # 1. Створюємо запис у таблиці Users
    user_fields = {
        'Full Name': full_name,
        'Email': email,
        'Role': role,
        'Password': password
    }
    new_user = users_table.create(user_fields)
    user_id = new_user['id']

    # 2. Якщо роль 'child' (учень) — створюємо додатково запис у таблиці 'Учні'
    if role == 'child':
        student_fields = {
            "Ім'я учня": full_name,
            'Учень': [user_id]
        }
        if class_name:
            student_fields['Клас'] = class_name
        students_table.create(student_fields)

    return jsonify({'status': 'success', 'user': new_user})




@app.route('/admin/update_user', methods=['POST'])
def admin_update_user():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    data = request.json
    user_id = data.get('user_id')
    full_name = data.get('full_name')
    email = data.get('email')
    role = data.get('role')
    password = data.get('password')

    if not user_id:
        return jsonify({'status': 'error', 'message': 'Відсутній ID користувача'}), 400

    update_fields = {}
    if full_name:
        update_fields['Full Name'] = full_name
    if email:
        update_fields['Email'] = email
    if role:
        update_fields['Role'] = role
    if password:  # Пароль оновлюємо тільки якщо його ввели в формі
        update_fields['Password'] = password

    updated_user = users_table.update(user_id, update_fields)
    return jsonify({'status': 'success', 'user': updated_user})



@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

    data = request.json
    user_id = data.get('user_id')

    if not user_id:
        return jsonify({'status': 'error', 'message': 'Відсутній ID користувача'}), 400

    # Шукаємо та видаляємо пов'язаний запис з таблиці 'Учні' (якщо є)
    students = students_table.all()
    for st in students:
        linked_users = st['fields'].get('Учень', [])
        if user_id in linked_users:
            students_table.delete(st['id'])
            break

    # Видаляємо запис з таблиці 'Users'
    users_table.delete(user_id)

    return jsonify({'status': 'success'})


@app.route('/admin/create_subject', methods=['POST'])
def create_subject():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': 'Доступ заборонено'}), 403

    name = request.form.get('name')
    if not name:
        return jsonify({'status': 'error', 'message': 'Введіть назву предмета'}), 400

    # Запис у таблицю 'Предмети' (поле 'Назва предмета')
    subjects_table.create({'Назва предмета': name})
    return jsonify({'status': 'success'})


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        # Шукаємо користувача в Airtable за Email
        records = users_table.all(formula=f"{{Email}} = '{email}'")
        if records:
            user = records[0]
            user_id = user['id']
            
            # Генеруємо токен із зашифрованим ID користувача
            token = serializer.dumps(user_id, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)
            
            # Формуємо та надсилаємо лист
            msg = Message("Скидання пароля", recipients=[email])
            msg.body = f"Вітаємо! Для скидання пароля перейдіть за цим посиланням: {reset_url}\n\nПосилання дійсне протягом 30 хвилин."
            try:
                mail.send(msg)
                flash('Лист із інструкціями для скидання пароля надіслано на вашу пошту.', 'success')
            except Exception as e:
                flash('Помилка при надсиланні листа. Перевірте конфігурацію пошти.', 'danger')
        else:
            # Із міркувань безпеки кажемо те саме, щоб не розкривати наявність пошти в базі
            flash('Якщо цей Email зареєстрований у системі, ви отримаєте лист із інструкціями.', 'info')
            
        return redirect(url_for('login'))
        
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        # Перевіряємо токен (максимальний час дії — 1800 секунд / 30 хвилин)
        user_id = serializer.loads(token, salt='password-reset-salt', max_age=1800)
    except (SignatureExpired, BadTimeSignature):
        flash('Посилання для скидання пароля недійсне або його термін дії закінчився.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not new_password or new_password != confirm_password:
            flash('Паролі не збігаються!', 'danger')
            return render_template('reset_password.html', token=token)

        
        users_table.update(user_id, {'Password': new_password})

        flash('Ваш пароль успішно змінено! Тепер ви можете увійти.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)



@app.route('/add_grade', methods=['POST'])
def add_grade():
    if session.get('role') != 'teacher' and session.get('role') != 'admin':
        return redirect(url_for('login'))
    
    try:
        subject_id = request.form.get('subject_id', '').strip()
        grade_date = request.form.get('grade_date')
        student_ids = request.form.getlist('student_ids[]')
        
        # Перевірка: предмет має бути обов'язково вибраний
        if not subject_id or not student_ids:
            return f"<h3>Помилка: Не обрано предмет або не вибрано жодного учня!</h3><br><a href='/teacher'>Повернутися назад</a>", 400

        records_to_create = []

        for st_id in student_ids:
            st_id = str(st_id).strip()
            if not st_id:
                continue

            status = request.form.get(f'status_{st_id}', 'Присутній')
            grade_val = request.form.get(f'grade_{st_id}', '').strip()
            comment_val = request.form.get(f'comment_{st_id}', '').strip()

            if status == 'Присутній' and not grade_val:
                return f"<h3>Помилка: Для всіх присутніх учнів обов'язково має бути виставлена оцінка!</h3><br><a href='/teacher'>Повернутися назад</a>", 400

            # Формуємо payload з масивами ID для полів зв'язку Link to Record
            payload = {
                'Учень': [st_id],
                'Предмет': [subject_id],
                'Статус': str(status)
            }

            if grade_date:
                payload['Дата виставлення оцінки'] = grade_date
            if grade_val:
                payload['Оцінка'] = int(grade_val)
            if comment_val:
                payload['Коментар вчителя'] = comment_val

            records_to_create.append(payload)

        if records_to_create:
            grades_table.batch_create(records_to_create)

        return redirect(url_for('teacher_dashboard'))

    except Exception as e:
        return f"<h3>Виникла помилка під час збереження:</h3><pre>{str(e)}</pre><br><a href='/teacher'>Повернутися назад</a>", 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/update_grade', methods=['POST'])
def update_grade():
    if session.get('role') != 'teacher' and session.get('role') != 'admin':
        return {'status': 'error', 'message': 'Недостатньо прав'}, 403

    try:
        record_id = request.form.get('record_id')
        grade_val = request.form.get('grade')
        status = request.form.get('status')
        comment = request.form.get('comment')
        grade_date = request.form.get('date')
        subject_id = request.form.get('subject_id')

        if not record_id:
            return {'status': 'error', 'message': 'ID запису відсутній'}, 400

        fields = {
            'Статус': str(status),
            'Коментар вчителя': comment if comment else ''
        }

        if status == 'Не присутній':
            fields['Оцінка'] = None
        elif grade_val:
            fields['Оцінка'] = int(grade_val)

        if grade_date:
            fields['Дата виставлення оцінки'] = grade_date

        if subject_id:
            fields['Предмет'] = [subject_id]

        grades_table.update(record_id, fields)
        return {'status': 'success'}

    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/delete_grade', methods=['POST'])
def delete_grade():
    if session.get('role') != 'teacher' and session.get('role') != 'admin':
        return {'status': 'error', 'message': 'Недостатньо прав'}, 403

    try:
        record_id = request.form.get('record_id')

        if not record_id:
            return {'status': 'error', 'message': 'ID запису відсутній'}, 400

        # Видалення запису з Airtable
        grades_table.delete(record_id)
        return {'status': 'success'}

    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/add_single_grade', methods=['POST'])
def add_single_grade():
    student_name = request.form.get('student_name')
    subject_id = request.form.get('subject_id')
    grade_date = request.form.get('date')
    status = request.form.get('status')
    grade = request.form.get('grade')
    comment = request.form.get('comment')

    # Пошук запису учня за іменем для отримання його ID
    students = students_table.all(formula=f"{{Ім'я учня}} = '{student_name}'")
    
    fields = {
        'Предмет': [subject_id],
        'Дата виставлення оцінки': grade_date,
        'Статус': status,
        'Коментар вчителя': comment or ''
    }
    
    if students:
        fields['Учень'] = [students[0]['id']]
        
    if grade and status != 'Не присутній':
        fields['Оцінка'] = int(grade)

    grades_table.create(fields)
    return jsonify({'status': 'success'})


if __name__ == '__main__':
    app.run(debug=True)
