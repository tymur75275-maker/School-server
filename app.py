import os
from flask import Flask, render_template, request, redirect, url_for, session
from pyairtable import Api

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
    
    # Отримуємо всі оцінки поточного учня
    records = grades_table.all(formula=f"{{Email учня}} = '{session['user']}'")
    
    dates_set = set()
    subjects_set = set()
    raw_grades = []

    for record in records:
        fields = record['fields']
        subject = clean_value(fields.get('Назва предмета')) or clean_value(fields.get('Предмет'))
        grade = clean_value(fields.get('Оцінка'))
        comment = clean_value(fields.get('Коментар вчителя'))
        date = clean_value(fields.get('Дата виставлення оцінки')) or clean_value(fields.get('Дата'))
        status = clean_value(fields.get('Статус'))

        if subject and date:
            subjects_set.add(subject)
            dates_set.add(date)
            raw_grades.append({
                'subject': subject,
                'date': date,
                'grade': grade,
                'status': status,
                'comment': comment
            })

    dates_list = sorted(list(dates_set))
    subjects_list = sorted(list(subjects_set))

    # Створюємо матрицю: matrix[subject][date] = grade_info
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
    
    # 1. Отримуємо всі оцінки для матриці
    records = grades_table.all()
    students_set = set()
    subjects_set = set()
    raw_grades = []

    for record in records:
        fields = record['fields']
        student = clean_value(fields.get("Ім'я учня")) or clean_value(fields.get('Учень'))
        subject = clean_value(fields.get('Назва предмета')) or clean_value(fields.get('Предмет'))
        grade = clean_value(fields.get('Оцінка'))
        status = clean_value(fields.get('Статус'))

        if student and subject:
            students_set.add(student)
            subjects_set.add(subject)
            raw_grades.append({'student': student, 'subject': subject, 'grade': grade or status})

    students_matrix_list = sorted(list(students_set))
    subjects_matrix_list = sorted(list(subjects_set))

    # Будуємо матрицю журналу для вчителя: matrix[student][subject] = grade
    matrix = {st: {subj: [] for subj in subjects_matrix_list} for st in students_matrix_list}
    for g in raw_grades:
        if g['grade']:
            matrix[g['student']][g['subject']].append(str(g['grade']))

    # 2. Список предметов для форми виставлення
    subject_records = subjects_table.all()
    subjects_list = []
    for s in subject_records:
        s_id = s['id']
        s_name = clean_value(s['fields'].get('Назва предмета'))
        if s_name:
            subjects_list.append((s_id, s_name))

    # 3. Список учнів для форми виставлення
    student_records = students_table.all()
    students_list = []
    for st in student_records:
        st_id = st['id']
        st_name = clean_value(st['fields'].get("Ім'я учня"))
        st_class = clean_value(st['fields'].get("Клас"))
        if st_name:
            students_list.append((st_id, st_name, st_class))
            
    return render_template('teacher.html', 
                           matrix=matrix, 
                           matrix_students=students_matrix_list, 
                           matrix_subjects=subjects_matrix_list, 
                           subjects=subjects_list, 
                           students=students_list)
@app.route('/add_grade', methods=['GET', 'POST'])
def add_grade():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    if request.method == 'GET':
        return redirect(url_for('teacher_dashboard'))
    
    try:
        subject_id = request.form.get('subject_id')
        student_ids = request.form.getlist('student_ids[]')
        
        if not subject_id or not student_ids:
            return redirect(url_for('teacher_dashboard'))

        records_to_create = []

        # Проходимо по кожному позначеному учню з форми
        for st_id in student_ids:
            # Перевіряємо, щоб st_id не був порожнім
            if not st_id or str(st_id).strip() == '':
                continue

            status = request.form.get(f'status_{st_id}', 'Присутній')
            grade_val = request.form.get(f'grade_{st_id}', '').strip()
            comment_val = request.form.get(f'comment_{st_id}', '').strip()

            # Валідація: якщо Присутній — оцінка обов'язкова
            if status == 'Присутній' and not grade_val:
                return f"<h3>Помилка: Для всіх присутніх учнів обов'язково має бути виставлена оцінка!</h3><br><a href='/teacher'>Повернутися назад</a>", 400

            # Передаємо саме масиви ID для полів типу 'Link to another record'
            payload = {
                'Учень': [st_id],
                'Предмет': [subject_id],
                'Статус': str(status)
            }

            if grade_val:
                payload['Оцінка'] = int(grade_val)
            if comment_val:
                payload['Коментар вчителя'] = comment_val

            records_to_create.append(payload)

        # Масове створення записів у Airtable
        if records_to_create:
            grades_table.batch_create(records_to_create)

        return redirect(url_for('teacher_dashboard'))

    except Exception as e:
        return f"<h3>Виникла помилка під час збереження:</h3><pre>{str(e)}</pre><br><a href='/teacher'>Повернутися назад</a>", 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
