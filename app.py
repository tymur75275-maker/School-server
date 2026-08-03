import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)
# Секретний ключ для сесій (зчитуємо із змінних оточення або ставимо дефолтний для тесту)
app.secret_key = os.environ.get('SECRET_KEY', 'default_secret_key_for_dev')

def init_db():
    conn = sqlite3.connect('grades.db')
    cursor = conn.cursor()
    # Таблиця користувачів
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')
    # Таблиця оцінок
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
        
        # Тут буде перевірка користувача з бази даних
        # Тимчасова заглушка для перевірки:
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
    return f"Вітаємо, учень {session['user']}! Тут будуть ваші оцінки."

@app.route('/teacher')
def teacher_dashboard():
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    return f"Вітаємо, вчителю {session['user']}! Тут буде панель керування оцінками."

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
