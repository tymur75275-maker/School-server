import os
from flask import Flask, render_template, request, redirect, url_for, session, flash
from pyairtable import Api

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-12345")

# Зчитуємо ключі з змінних оточення (або значення за замовчуванням, якщо вони не задані)
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")

api = Api(AIRTABLE_API_KEY)

grades_table = api.table(AIRTABLE_BASE_ID, 'Оцінки')
users_table = api.table(AIRTABLE_BASE_ID, 'Users')
subjects_table = api.table(AIRTABLE_BASE_ID, 'Предмети')
students_table = api.table(AIRTABLE_BASE_ID, 'Учні')

def clean_val(val):
    if isinstance(val, list):
        return val[0] if val else ""
    return val or ""

@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = str(request.form.get('password', '')).strip()

        all_users = users_table.all()
        user_match = None

        for u in all_users:
            fields = u.get('fields', {})
            
            # Надійно отримуємо значення email та пароля з Airtable
            raw_email = fields.get('Email') or fields.get('email') or ''
            raw_pass = fields.get('Пароль') or fields.get('пароль') or fields.get('Password') or ''

            u_email = str(raw_email).strip().lower()
            u_pass = str(raw_pass).strip()

            if u_email == email and u_pass == password:
                user_match = fields
                break

        if user_match:
            session['user_email'] = email
            session['user_role'] = user_match.get('Роль')
            session['user_name'] = user_match.get('ПІБ') or email

            if session['user_role'] == 'Вчитель':
                return redirect(url_for('teacher_dashboard'))
            else:
                return redirect(url_for('student_dashboard'))
        else:
            flash("Невірний email або пароль!", "danger")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/teacher', methods=['GET', 'POST'])
def teacher_dashboard():
    if session.get('user_role') != 'Вчитель':
        return redirect(url_for('login'))

    subjects = subjects_table.all()
    students = students_table.all()

    selected_subject = request.args.get('subject')
    if not selected_subject and subjects:
        selected_subject = subjects[0]['fields'].get('Назва', '')

    if request.method == 'POST':
        date = request.form.get('date')
        subject = request.form.get('subject')
        topic = request.form.get('topic', '')

        for st in students:
            st_id = st['id']
            status = request.form.get(f'status_{st_id}', 'Присутній')
            grade = request.form.get(f'grade_{st_id}', '').strip()
            comment = request.form.get(f'comment_{st_id}', '').strip()

            if grade or status != 'Присутній' or comment:
                fields = {
                    'Учень': [st_id],
                    'Предмет': [s['id'] for s in subjects if s['fields'].get('Назва') == subject],
                    'Дата': date,
                    'Статус': status,
                    'Тема': topic
                }
                if grade:
                    fields['Оцінка'] = int(grade) if grade.isdigit() else grade
                if comment:
                    fields['Коментар'] = comment

                grades_table.create(fields)

        flash("Оцінки успішно виставлено!", "success")
        return redirect(url_for('teacher_dashboard', subject=subject))

    all_grades = grades_table.all()
    
    subject_grades = []
    active_dates = set()

    for g in all_grades:
        f = g.get('fields', {})
        subj_name = clean_val(f.get('Назва предмета')) or clean_val(f.get('Предмет'))
        if subj_name == selected_subject:
            d = f.get('Дата')
            if d:
                active_dates.add(d)
            subject_grades.append(f)

    sorted_dates = sorted(list(active_dates))

    grid_data = {}
    for st in students:
        st_id = st['id']
        st_name = st['fields'].get('ПІБ') or st['fields'].get('Ім\'я') or 'Учень'
        grid_data[st_id] = {
            'name': st_name,
            'grades': {}
        }

    for g in subject_grades:
        st_id = clean_val(g.get('Учень'))
        d = g.get('Дата')
        if st_id in grid_data and d:
            grid_data[st_id]['grades'][d] = {
                'grade': g.get('Оцінка', ''),
                'status': g.get('Статус', 'Присутній'),
                'comment': g.get('Коментар', ''),
                'topic': g.get('Тема', '')
            }

    return render_template(
        'teacher.html',
        subjects=subjects,
        students=students,
        selected_subject=selected_subject,
        dates=sorted_dates,
        grid_data=grid_data
    )

@app.route('/student')
def student_dashboard():
    if session.get('user_role') != 'Учень':
        return redirect(url_for('login'))

    user_email = session.get('user_email')
    all_students = students_table.all()
    current_student = None

    for st in all_students:
        f = st.get('fields', {})
        if str(f.get('Email', '')).strip().lower() == user_email:
            current_student = st
            break

    if not current_student:
        flash("Профіль учня не знайдено!", "danger")
        return redirect(url_for('login'))

    st_id = current_student['id']
    st_name = current_student['fields'].get('ПІБ') or session.get('user_name')

    all_grades = grades_table.all()
    all_subjects = subjects_table.all()
    subject_names = [s['fields'].get('Назва') for s in all_subjects if 'Назва' in s['fields']]

    active_dates = set()
    student_grades = []

    for g in all_grades:
        f = g.get('fields', {})
        rec_st_id = clean_val(f.get('Учень'))
        if rec_st_id == st_id:
            d = f.get('Дата')
            if d:
                active_dates.add(d)
            student_grades.append(f)

    sorted_dates = sorted(list(active_dates))

    grid_data = {subj: {} for subj in subject_names}

    for g in student_grades:
        subj = clean_val(g.get('Назва предмета')) or clean_val(g.get('Предмет'))
        d = g.get('Дата')
        if subj in grid_data and d:
            grid_data[subj][d] = {
                'grade': g.get('Оцінка', ''),
                'status': g.get('Статус', 'Присутній'),
                'comment': g.get('Коментар', ''),
                'topic': g.get('Тема', '')
            }

    return render_template(
        'student.html',
        student_name=st_name,
        dates=sorted_dates,
        grid_data=grid_data
    )

if __name__ == '__main__':
    app.run(debug=True)
