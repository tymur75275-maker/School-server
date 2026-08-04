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

@app.route('/')
def clean_value(val):
    """Якщо значення прийшло як список ['...'], витягуємо перший елемент"""
    if isinstance(val, list) and len(val) > 0:
        return val[0]
    return val if val is not None else ''
def home():
    if 'user' in session:
        if session['role'] == 'teacher':
            return redirect(url_for('teacher_dashboard'))
        return redirect(url_for('student_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Тимчасова перевірка входу
        if email == 'teacher@school.com' and password == 'admin123':
            session['user'] = email
            session['role'] = 'teacher'
            return redirect(url_for('teacher_dashboard'))
        elif email and password:
            session['user'] = email
            session['role'] = 'student'
            return redirect(url_for('student_dashboard'))
            
    return render_template('login.html')

@app.route('/student')
def student_dashboard():
    if session.get('role') != 'student':
        return redirect(url_for('login'))
    
    # Витягуємо оцінки поточного учня з Airtable за його Email
    records = grades_table.all(formula=f"{{Email учня}} = '{session['user']}'")
    
    student_grades = []
    for record in records:
        fields = record['fields']
        # Очищаємо від списків ['...']
        subject = clean_value(fields.get('Предмет'))
        grade = clean_value(fields.get('Оцінка'))
        student_grades.append((subject, grade))
    
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
        subject = clean_value(fields.get('Предмет'))
        grade = clean_value(fields.get('Оцінка'))
        all_grades.append((student, subject, grade))
    
    return render_template('teacher.html', grades=all_grades)

@app.route('/add_grade', methods=['POST'])
def add_grade():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    student_email = request.form.get('student_email')
    subject = request.form.get('subject')
    grade = request.form.get('grade')
    
    if student_email and subject and grade:
        # Записуємо нову оцінку в Airtable з точними назвами полів
        grades_table.create({
            'Email учня': student_email,
            'Предмет': subject,
            'Оцінка': int(grade)
        })
        
    return redirect(url_for('teacher_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
