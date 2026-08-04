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
    
    # Витягуємо оцінки поточного учня з Airtable за його Email
    records = grades_table.all(formula=f"{{Email учня}} = '{session['user']}'")
    
    student_grades = []
    for record in records:
        fields = record['fields']
        subject = clean_value(fields.get('Назва предмета'))  # Або 'Предмет', якщо берете Lookup
        grade = clean_value(fields.get('Оцінка'))
        comment = clean_value(fields.get('Коментар вчителя'))
        date = clean_value(fields.get('Дата виставлення оцінки'))
        
        student_grades.append((subject, grade, date, comment))
    
    return render_template('student.html', grades=student_grades, email=session['user'])

@app.route('/teacher')
def teacher_dashboard():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    # Витягуємо всі оцінки з Airtable для вчителя
    records = grades_table.all()
    
    all_grades = []
    for record in records:
        fields = record['fields']
        # Очищаємо всі поля від списків ['...']
        student = clean_value(fields.get('Email учня'))
        subject = clean_value(fields.get('Назва предмета'))
        grade = clean_value(fields.get('Оцінка'))
        all_grades.append((student, subject, grade))
    
    return render_template('teacher.html', grades=all_grades)

@app.route('/add_grade', methods=['GET', 'POST'])
def add_grade():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    if request.method == 'GET':
        return redirect(url_for('teacher_dashboard'))
    
    student_email = request.form.get('student_email')
    subject_name = request.form.get('subject')  # Текст, наприклад "Математика"
    grade = request.form.get('grade')
    
    if student_email and subject_name and grade:
        # Шукаємо Record ID предмета за його назвою в таблиці "Предмети"
        # Переконайтеся, що колонка в таблиці "Предмети" називається 'Назва' або 'Предмет'
        subject_records = subjects_table.all(formula=f"{{Назва предмета}} = '{subject_name}'")
        
        if subject_records:
            subject_id = subject_records[0]['id']
            
            # Записуємо оцінку, передаючи ID у вигляді списку [subject_id]
            grades_table.create({
                'Email учня': student_email,
                'Предмет': [subject_id],
                'Оцінка': int(grade)
            })
        
    return redirect(url_for('teacher_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
