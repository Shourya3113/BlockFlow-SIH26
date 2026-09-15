from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import pickle
import numpy as np

app = Flask(__name__)
CORS(app)

# Load model
with open("student_model.pkl", "rb") as f:
    model = pickle.load(f)

# DB setup (SQLite)
def init_db():
    conn = sqlite3.connect("student.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS students
                 (id INTEGER PRIMARY KEY, name TEXT, attendance INTEGER, marks INTEGER)''')
    conn.commit()
    conn.close()

init_db()

@app.route("/api/add_student", methods=["POST"])
def add_student():
    data = request.json
    conn = sqlite3.connect("student.db")
    c = conn.cursor()
    c.execute("INSERT INTO students (name, attendance, marks) VALUES (?, ?, ?)",
              (data["name"], data["attendance"], data["marks"]))
    conn.commit()
    conn.close()
    return jsonify({"message": "Student added successfully"}), 201

@app.route("/api/students", methods=["GET"])
def get_students():
    conn = sqlite3.connect("student.db")
    c = conn.cursor()
    c.execute("SELECT * FROM students")
    rows = c.fetchall()
    conn.close()
    students = [{"id": r[0], "name": r[1], "attendance": r[2], "marks": r[3]} for r in rows]
    return jsonify(students)

@app.route("/api/predict", methods=["POST"])
def predict():
    data = request.json
    features = np.array([[data["attendance"], data["marks"]]])
    prediction = model.predict(features)[0]
    result = "Pass" if prediction == 1 else "Fail"
    return jsonify({"prediction": result})

if __name__ == "__main__":
    app.run(debug=True)
