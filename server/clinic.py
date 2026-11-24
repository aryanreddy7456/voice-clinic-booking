"""Mock clinic data and booking tools.

Everything here is fake demo data: fake doctors, fake slots, fake patients.
The voice agent identifies a caller only by the phone number they speak
during the call. There is no login and no real patient information.

Storage is a small SQLite file so bookings survive while the server runs.
Run `python server/clinic.py` once to create and seed the database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic.db"

# Fake doctors and their weekly slots. Times are 24-hour "HH:MM" strings.
DOCTORS = [
    {
        "id": 1,
        "name": "Dr. Ananya Sharma",
        "speciality": "General Physician",
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "start": "09:00",
        "end": "13:00",
        "slot_minutes": 30,
    },
    {
        "id": 2,
        "name": "Dr. Rohan Mehta",
        "speciality": "Dentist",
        "days": ["Monday", "Wednesday", "Friday"],
        "start": "10:00",
        "end": "14:00",
        "slot_minutes": 30,
    },
    {
        "id": 3,
        "name": "Dr. Priya Nair",
        "speciality": "Dermatologist",
        "days": ["Tuesday", "Thursday", "Saturday"],
        "start": "11:00",
        "end": "15:00",
        "slot_minutes": 30,
    },
    {
        "id": 4,
        "name": "Dr. Vikram Rao",
        "speciality": "Pediatrician",
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
        "start": "16:00",
        "end": "20:00",
        "slot_minutes": 30,
    },
]

# How many days ahead the demo clinic takes bookings.
BOOKING_WINDOW_DAYS = 7


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and seed the doctors once."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            speciality TEXT NOT NULL,
            days TEXT NOT NULL,
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            slot_minutes INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id INTEGER NOT NULL REFERENCES doctors(id),
            patient_name TEXT,
            phone TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'booked',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER NOT NULL REFERENCES calls(id),
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    cur.execute("SELECT COUNT(*) AS n FROM doctors")
    if cur.fetchone()["n"] == 0:
        for d in DOCTORS:
            cur.execute(
                "INSERT INTO doctors (id, name, speciality, days, start, end, slot_minutes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    d["id"],
                    d["name"],
                    d["speciality"],
                    ",".join(d["days"]),
                    d["start"],
                    d["end"],
                    d["slot_minutes"],
                ),
            )
    conn.commit()
    conn.close()


def list_doctors() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM doctors ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _doctor_slots_on(conn: sqlite3.Connection, doctor: sqlite3.Row, date_str: str, weekday: str) -> list[str]:
    """All start times for a doctor on one date, minus already-booked ones."""
    if weekday not in doctor["days"].split(","):
        return []
    start_h, start_m = map(int, doctor["start"].split(":"))
    end_h, end_m = map(int, doctor["end"].split(":"))
    step = doctor["slot_minutes"]
    times = []
    minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    while minutes + step <= end_minutes:
        times.append(f"{minutes // 60:02d}:{minutes % 60:02d}")
        minutes += step
    booked = {
        r["time"]
        for r in conn.execute(
            "SELECT time FROM appointments WHERE doctor_id = ? AND date = ? AND status = 'booked'",
            (doctor["id"], date_str),
        )
    }
    return [t for t in times if t not in booked]


def check_doctor_availability(doctor_name: str | None = None, date: str | None = None) -> dict:
    """Tool 1: free slots for a doctor and/or a date.

    doctor_name: part of the doctor's name or speciality, e.g. "Sharma" or "dentist".
    date: YYYY-MM-DD. When omitted, the next few working days are shown.
    """
    from datetime import datetime, timedelta

    conn = _connect()
    doctors = conn.execute("SELECT * FROM doctors ORDER BY name").fetchall()

    if doctor_name:
        needle = doctor_name.strip().lower()
        doctors = [
            d
            for d in doctors
            if needle in d["name"].lower() or needle in d["speciality"].lower()
        ]
        if not doctors:
            conn.close()
            return {
                "found": False,
                "message": f"No doctor matches '{doctor_name}'. "
                "Ask the caller which speciality they need.",
            }

    if date:
        days = [datetime.strptime(date, "%Y-%m-%d").date()]
    else:
        today = datetime.now().date()
        days = [today + timedelta(days=i) for i in range(1, BOOKING_WINDOW_DAYS + 1)]

    result = []
    for d in doctors:
        for day in days:
            slots = _doctor_slots_on(conn, d, day.isoformat(), day.strftime("%A"))
            if slots:
                result.append(
                    {
                        "doctor": d["name"],
                        "speciality": d["speciality"],
                        "date": day.isoformat(),
                        "weekday": day.strftime("%A"),
                        "free_slots": slots,  # full list: what the dashboard shows is exactly what is bookable
                    }
                )
    conn.close()
    if not result:
        return {"found": False, "message": "No free slots in that range. Try another date."}
    return {"found": True, "availability": result[:8]}


def book_appointment(phone: str, doctor_name: str, date: str, time: str, patient_name: str | None = None) -> dict:
    """Tool 2: book one slot for the caller.

    phone: the phone number the caller spoke during the call (digits only).
    date: YYYY-MM-DD, time: HH:MM (24-hour).
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 6:
        return {"booked": False, "message": "That phone number looks too short. Ask the caller to repeat it."}

    conn = _connect()
    needle = doctor_name.strip().lower()
    doctor = None
    for d in conn.execute("SELECT * FROM doctors"):
        if needle in d["name"].lower() or needle in d["speciality"].lower():
            doctor = d
            break
    if doctor is None:
        conn.close()
        return {"booked": False, "message": f"No doctor matches '{doctor_name}'."}

    from datetime import datetime

    weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
    free = _doctor_slots_on(conn, doctor, date, weekday)
    if time not in free:
        conn.close()
        # Suggest the free slots closest to what the caller asked for.
        try:
            asked = int(time[:2]) * 60 + int(time[3:])
            nearest = sorted(free, key=lambda t: abs(int(t[:2]) * 60 + int(t[3:]) - asked))[:3]
        except (ValueError, IndexError):
            nearest = free[:3]
        return {
            "booked": False,
            "message": f"That slot is not free. Nearest free slots that day: {', '.join(nearest) or 'none'}. "
            f"All free slots that day: {', '.join(free) or 'none'}.",
        }

    cur = conn.execute(
        "INSERT INTO appointments (doctor_id, patient_name, phone, date, time) VALUES (?, ?, ?, ?, ?)",
        (doctor["id"], patient_name or "Caller", digits, date, time),
    )
    conn.commit()
    appt_id = cur.lastrowid
    conn.close()
    return {
        "booked": True,
        "appointment_id": appt_id,
        "message": f"Booked {doctor['name']} ({doctor['speciality']}) on {date} at {time} for phone {digits}.",
    }


def cancel_appointment(phone: str) -> dict:
    """Tool 3: cancel the caller's appointment, found by their spoken phone number."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    conn = _connect()
    rows = conn.execute(
        """
        SELECT a.id, a.date, a.time, d.name AS doctor
        FROM appointments a JOIN doctors d ON d.id = a.doctor_id
        WHERE a.phone = ? AND a.status = 'booked'
        ORDER BY a.date, a.time
        """,
        (digits,),
    ).fetchall()
    if not rows:
        conn.close()
        return {"cancelled": False, "message": "No booked appointment found for that phone number."}
    row = rows[0]
    conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return {
        "cancelled": True,
        "message": f"Cancelled the appointment with {row['doctor']} on {row['date']} at {row['time']}.",
    }


def delete_appointment(appointment_id: int) -> bool:
    """Admin: remove one appointment row completely.

    The row is deleted (not marked cancelled), so its time slot becomes free
    again for new bookings.
    """
    conn = _connect()
    cur = conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def start_call() -> int:
    """Open a transcript record for one browser call. Returns the call id."""
    from datetime import datetime

    conn = _connect()
    cur = conn.execute("INSERT INTO calls (started_at) VALUES (?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    call_id = cur.lastrowid
    conn.close()
    return call_id


def finish_call(call_id: int) -> None:
    from datetime import datetime

    conn = _connect()
    conn.execute("UPDATE calls SET ended_at = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), call_id))
    conn.commit()
    conn.close()


def add_transcript_line(call_id: int, role: str, content: str) -> None:
    from datetime import datetime

    conn = _connect()
    conn.execute(
        "INSERT INTO transcript_lines (call_id, ts, role, content) VALUES (?, ?, ?, ?)",
        (call_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), role, content),
    )
    conn.commit()
    conn.close()


def list_calls() -> list[dict]:
    """All calls, newest first, with how many transcript lines each has."""
    conn = _connect()
    rows = conn.execute(
        """
        SELECT c.id, c.started_at, c.ended_at, COUNT(t.id) AS line_count
        FROM calls c LEFT JOIN transcript_lines t ON t.call_id = c.id
        GROUP BY c.id ORDER BY c.id DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_call_transcript(call_id: int) -> dict | None:
    conn = _connect()
    call = conn.execute("SELECT id, started_at, ended_at FROM calls WHERE id = ?", (call_id,)).fetchone()
    if call is None:
        conn.close()
        return None
    lines = conn.execute(
        "SELECT ts, role, content FROM transcript_lines WHERE call_id = ? ORDER BY id", (call_id,)
    ).fetchall()
    conn.close()
    return {"call": dict(call), "lines": [dict(r) for r in lines]}


def delete_call(call_id: int) -> bool:
    """Admin: delete one call and its whole transcript."""
    conn = _connect()
    conn.execute("DELETE FROM transcript_lines WHERE call_id = ?", (call_id,))
    cur = conn.execute("DELETE FROM calls WHERE id = ?", (call_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def update_doctor_schedule(doctor_id: int, days: list[str], start: str, end: str) -> dict:
    """Admin: change one doctor's working days and hours.

    days: weekday names like ["Monday", "Wednesday"]. start/end: "HH:MM" 24-hour.
    Slots are always generated from these values, so the dashboard, the
    availability tool and booking all follow the new timings at once.
    """
    from datetime import datetime

    valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days = [d for d in days if d in valid_days]
    if not days:
        return {"updated": False, "message": "Pick at least one working day."}
    try:
        start_m = int(start[:2]) * 60 + int(start[3:])
        end_m = int(end[:2]) * 60 + int(end[3:])
        datetime.strptime(start, "%H:%M")
        datetime.strptime(end, "%H:%M")
    except (ValueError, IndexError):
        return {"updated": False, "message": "Times must be HH:MM, 24-hour."}
    if end_m - start_m < 30:
        return {"updated": False, "message": "The day must be at least 30 minutes long."}

    conn = _connect()
    cur = conn.execute(
        "UPDATE doctors SET days = ?, start = ?, end = ? WHERE id = ?",
        (",".join(days), start, end, doctor_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    if not changed:
        return {"updated": False, "message": "Doctor not found."}
    return {"updated": True, "message": f"Schedule updated: {', '.join(days)}, {start}-{end}."}


def list_appointments() -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT a.id, d.name AS doctor, d.speciality, a.patient_name, a.phone,
               a.date, a.time, a.status
        FROM appointments a JOIN doctors d ON d.id = a.doctor_id
        ORDER BY a.date, a.time
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Seeded {DB_PATH}")
    print(check_doctor_availability(doctor_name="dentist"))
