import os
from flask import Flask, render_template, request, redirect, url_for, session
from pyairtable import Api
from datetime import date

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default_secret_key_for_dev')

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
        return redirect(url_for('student_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Перевірка: якщо користувач ВЖЕ авторизований, редиректимо його геть з логину
    if 'user' in session:
        if session.get('role') == 'teacher':
            return redirect(url_for('teacher_dashboard'))
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
                if str(user_role).lower() in ['teacher', 'admin']:
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

@app.route('/student')
def student_dashboard():
    if session.get('role') != 'student':
        return redirect(url_for('login'))
    
    current_user = session['user']
    records = grades_table.all()
    
    dates_set = set()
    subjects_set = set()
    raw_grades = []

    for record in records:
        fields = record['fields']
        
        # Перевіряємо email учня (з поля Email учня або через зв'язані поля)
        st_email = str(clean_value(fields.get('Email учня') or fields.get('Email') or '')).strip().lower()
        
        # Якщо email збігається або якщо email у полі не вказано взагалі
        if st_email == current_user.lower() or not st_email:
            subject = clean_value(fields.get('Назва предмета')) or clean_value(fields.get('Предмет'))
            grade = clean_value(fields.get('Оцінка'))
            comment = clean_value(fields.get('Коментар вчителя'))
            date = clean_value(fields.get('Дата виставлення оцінки')) or clean_value(fields.get('Дата')) or 'Без дати'
            status = clean_value(fields.get('Статус'))

            if subject:
                subjects_set.add(str(subject))
                dates_set.add(str(date))
                raw_grades.append({
                    'id': record['id'],
                    'subject': str(subject),
                    'date': str(date),
                    'grade': grade,
                    'status': status or 'Присутній',
                    'comment': comment or ''
                })

    dates_list = sorted(list(dates_set))
    subjects_list = sorted(list(subjects_set))

    # Створюємо матрицю: matrix[subject][date] = record
    matrix = {subj: {dt: None for dt in dates_list} for subj in subjects_list}
    for item in raw_grades:
        matrix[item['subject']][item['date']] = item

    return render_template('student.html', 
                           subjects=subjects_list, 
                           dates=dates_list, 
                           matrix=matrix, 
                           email=session['user'])

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
        today_date=today_str
    )

@app.route('/add_grade', methods=['POST'])
def add_grade():
    if session.get('role') != 'teacher':
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
    if session.get('role') != 'teacher':
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
    if session.get('role') != 'teacher':
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

if __name__ == '__main__':
    app.run(debug=True)
