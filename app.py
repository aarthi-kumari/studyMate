from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from functools import wraps
from typing import Any, Callable, TypeVar

from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

DB_FILENAME = "studymate.db"


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.secret_key = os.environ.get("STUDYMATE_SECRET", "dev-secret")

    Handler = TypeVar("Handler", bound=Callable[..., Any])

    def get_db() -> sqlite3.Connection:
        if not hasattr(app, "_db"):
            db_path = os.path.join(app.instance_path, DB_FILENAME)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            app._db = conn
        return app._db  # type: ignore[return-value]

    def add_column_if_missing(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = db.execute(f"PRAGMA table_info({table})").fetchall()
        if any(col["name"] == column for col in columns):
            return
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def init_db() -> None:
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#2f6fed',
                target_hours INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                user_id INTEGER,
                title TEXT NOT NULL,
                due_date TEXT,
                est_minutes INTEGER NOT NULL DEFAULT 30,
                status TEXT NOT NULL DEFAULT 'todo',
                created_at TEXT NOT NULL,
                FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );
            """
        )
        add_column_if_missing(
            db,
            "subjects",
            "user_id",
            "user_id INTEGER REFERENCES users (id) ON DELETE CASCADE",
        )
        add_column_if_missing(
            db,
            "tasks",
            "user_id",
            "user_id INTEGER REFERENCES users (id) ON DELETE CASCADE",
        )
        db.commit()

    @app.teardown_appcontext
    def close_db(_: Any) -> None:
        db = getattr(app, "_db", None)
        if db is not None:
            db.close()
            delattr(app, "_db")

    @app.route("/")
    def index() -> str:
        init_db()
        db = get_db()
        user_id = session.get("user_id")

        if not user_id:
            return render_template(
                "index.html",
                subjects=[],
                tasks=[],
                overall_total=0,
                overall_done=0,
                overall_percent=0,
            )

        subjects = db.execute(
            """
            SELECT s.id, s.name, s.color, s.target_hours,
                   COUNT(t.id) AS task_total,
                   SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS task_done
            FROM subjects s
            LEFT JOIN tasks t ON t.subject_id = s.id
            WHERE s.user_id = ?
            GROUP BY s.id
            ORDER BY s.created_at DESC
            """,
            (user_id,),
        ).fetchall()

        tasks = db.execute(
            """
            SELECT t.id, t.title, t.due_date, t.est_minutes, t.status,
                   s.name AS subject_name, s.color AS subject_color
            FROM tasks t
            JOIN subjects s ON s.id = t.subject_id
            WHERE t.user_id = ?
            ORDER BY (t.status = 'done') ASC,
                     CASE WHEN t.due_date IS NULL OR t.due_date = '' THEN 1 ELSE 0 END ASC,
                     t.due_date ASC,
                     t.created_at DESC
            """,
            (user_id,),
        ).fetchall()

        overall_total = sum(row["task_total"] or 0 for row in subjects)
        overall_done = sum(row["task_done"] or 0 for row in subjects)
        overall_percent = (
            int((overall_done / overall_total) * 100) if overall_total else 0
        )

        return render_template(
            "index.html",
            subjects=subjects,
            tasks=tasks,
            overall_total=overall_total,
            overall_done=overall_done,
            overall_percent=overall_percent,
        )

    @app.route("/subjects", methods=["POST"])
    def add_subject() -> str:
        init_db()
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        name = request.form.get("name", "").strip()
        color = request.form.get("color", "#2f6fed").strip() or "#2f6fed"
        target_hours = request.form.get("target_hours", "0").strip() or "0"

        if name:
            db = get_db()
            db.execute(
                """
                INSERT INTO subjects (user_id, name, color, target_hours, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    name,
                    color,
                    int(target_hours),
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
        return redirect(url_for("index"))

    @app.route("/subjects/<int:subject_id>/delete", methods=["POST"])
    def delete_subject(subject_id: int) -> str:
        init_db()
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        db = get_db()
        db.execute(
            "DELETE FROM subjects WHERE id = ? AND user_id = ?",
            (subject_id, user_id),
        )
        db.commit()
        return redirect(url_for("index"))

    @app.route("/tasks", methods=["POST"])
    def add_task() -> str:
        init_db()
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        title = request.form.get("title", "").strip()
        subject_id = request.form.get("subject_id", "").strip()
        due_date = request.form.get("due_date", "").strip() or None
        est_minutes = request.form.get("est_minutes", "30").strip() or "30"

        if title and subject_id.isdigit():
            db = get_db()
            subject = db.execute(
                "SELECT id FROM subjects WHERE id = ? AND user_id = ?",
                (int(subject_id), user_id),
            ).fetchone()
            if not subject:
                return redirect(url_for("index"))
            db.execute(
                """
                INSERT INTO tasks (subject_id, user_id, title, due_date, est_minutes, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'todo', ?)
                """,
                (
                    int(subject_id),
                    int(user_id),
                    title,
                    due_date,
                    int(est_minutes),
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
        return redirect(url_for("index"))

    @app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
    def toggle_task(task_id: int) -> str:
        init_db()
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        db = get_db()
        current = db.execute(
            "SELECT status FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        if current:
            new_status = "todo" if current["status"] == "done" else "done"
            db.execute(
                "UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?",
                (new_status, task_id, user_id),
            )
            db.commit()
        return redirect(url_for("index"))

    @app.route("/tasks/<int:task_id>/delete", methods=["POST"])
    def delete_task(task_id: int) -> str:
        init_db()
        user_id = session.get("user_id")
        if not user_id:
            return redirect(url_for("login"))
        db = get_db()
        db.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        db.commit()
        return redirect(url_for("index"))

    def login_required(handler: Handler) -> Handler:
        @wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not session.get("user_id"):
                return redirect(url_for("login"))
            return handler(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    @app.context_processor
    def inject_user() -> dict[str, Any]:
        user = None
        user_id = session.get("user_id")
        if user_id:
            db = get_db()
            user = db.execute(
                "SELECT id, name, email FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return {"current_user": user}

    @app.route("/signup", methods=["GET", "POST"])
    def signup() -> str:
        init_db()
        error = None
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "").strip()

            if not name or not email or not password:
                error = "Please fill in all fields."
            else:
                db = get_db()
                existing = db.execute(
                    "SELECT id FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                if existing:
                    error = "Email already registered."
                else:
                    db.execute(
                        """
                        INSERT INTO users (name, email, password_hash, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            name,
                            email,
                            generate_password_hash(password),
                            datetime.utcnow().isoformat(),
                        ),
                    )
                    db.commit()
                    user_id = db.execute(
                        "SELECT id FROM users WHERE email = ?",
                        (email,),
                    ).fetchone()
                    if user_id:
                        session["user_id"] = user_id["id"]
                    return redirect(url_for("index"))

        return render_template("signup.html", error=error)

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str:
        init_db()
        error = None
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "").strip()

            if not email or not password:
                error = "Please enter your email and password."
            else:
                db = get_db()
                user = db.execute(
                    "SELECT id, password_hash FROM users WHERE email = ?",
                    (email,),
                ).fetchone()
                if not user or not check_password_hash(user["password_hash"], password):
                    error = "Invalid credentials."
                else:
                    session["user_id"] = user["id"]
                    return redirect(url_for("index"))

        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout() -> str:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
