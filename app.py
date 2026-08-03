import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default_secret_key_for_dev')

def init_db():
    conn = sqlite3.connect('grades.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            grade INTEGER NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
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
        
        # Тимчасова перевірка для тестування
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
    
    # Витягуємо оцінки ЛИШЕ поточного учня з бази
    conn = sqlite3.connect('grades.db')
    cursor = conn.cursor()
    cursor.execute('SELECT subject, grade FROM grades WHERE student_email = ?', (session['user'],))
    student_grades = cursor.fetchall()
    conn.close()
    
    return render_template('student.html', grades=student_grades, email=session['user'])

@app.route('/teacher')
def teacher_dashboard():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    # Витягуємо всі оцінки для перегляду вчителем
    conn = sqlite3.connect('grades.db')
    cursor = conn.cursor()
    cursor.execute('SELECT student_email, subject, grade FROM grades')
    all_grades = cursor.fetchall()
    conn.close()
    
    return render_template('teacher.html', grades=all_grades)

@app.route('/add_grade', methods=['POST'])
def add_grade():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    
    student_email = request.form.get('student_email')
    subject = request.form.get('subject')
    grade = request.form.get('grade')
    
    if student_email and subject and grade:
        conn = sqlite3.connect('grades.db')
        cursor = conn.cursor()
        cursor.execute('INSERT INTO grades (student_email, subject, grade) VALUES (?, ?, ?)',
                       (student_email, subject, int(grade)))
        conn.commit()
        conn.close()
        
    return redirect(url_for('teacher_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
