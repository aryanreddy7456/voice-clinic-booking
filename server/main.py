"""FastAPI server: serves the dashboard, the WebRTC offer endpoint, and REST/WebSocket APIs.

Run:  python server/main.py   (from the project root, with .env filled in)
Open: http://localhost:8000
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv(ROOT / ".env")

import clinic  # noqa: E402
import events  # noqa: E402
from bot import run_bot  # noqa: E402
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

logger.remove()
logger.add(sys.stderr, level=__import__("os").environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="City Care Clinic - Voice Booking Demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

webrtc_handler = SmallWebRTCRequestHandler()


@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest):
    """Small WebRTC handshake from the browser client."""

    async def on_connection(conn):
        # One voice bot per browser call.
        asyncio.create_task(run_bot(conn))

    return await webrtc_handler.handle_web_request(request, on_connection)


@app.patch("/api/offer")
async def offer_ice(request: SmallWebRTCPatchRequest):
    """Trickle ICE candidates from the browser into its connection."""
    await webrtc_handler.handle_patch_request(request)
    return {"status": "ok"}


@app.get("/api/doctors")
async def doctors():
    """Doctors with their free slots over the next days (for the dashboard)."""
    from datetime import datetime, timedelta

    result = []
    for doc in clinic.list_doctors():
        upcoming = []
        today = datetime.now().date()
        for i in range(1, clinic.BOOKING_WINDOW_DAYS + 1):
            day = today + timedelta(days=i)
            avail = clinic.check_doctor_availability(doctor_name=doc["name"], date=day.isoformat())
            if avail.get("found"):
                entry = avail["availability"][0]
                upcoming.append(
                    {
                        "date": entry["date"],
                        "weekday": entry["weekday"],
                        "free_slots": entry["free_slots"],
                    }
                )
        result.append(
            {
                "id": doc["id"],
                "name": doc["name"],
                "speciality": doc["speciality"],
                "upcoming": upcoming[:3],
            }
        )
    return {"doctors": result}


@app.get("/api/appointments")
async def appointments():
    return {"appointments": clinic.list_appointments()}


# --- Admin page (staff only) -------------------------------------------------
# A very simple shared-password check, fine for this local demo: the admin page
# sends the password from .env (ADMIN_TOKEN) in a header with every request.
# This is NOT real security - it only keeps casual visitors out of /admin.
ADMIN_TOKEN = __import__("os").environ.get("ADMIN_TOKEN", "clinic-admin-123")


def _check_admin(x_admin_token: str | None) -> None:
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Wrong or missing admin token")


@app.get("/api/admin/appointments")
async def admin_appointments(x_admin_token: str = Header(None)):
    """All appointments (any status) for the admin page."""
    _check_admin(x_admin_token)
    return {"appointments": clinic.list_appointments()}


@app.delete("/api/admin/appointments/{appointment_id}")
async def admin_delete_appointment(appointment_id: int, x_admin_token: str = Header(None)):
    """Delete one appointment row; its time slot becomes free again."""
    _check_admin(x_admin_token)
    if not clinic.delete_appointment(appointment_id):
        raise HTTPException(status_code=404, detail="Appointment not found")
    return {"deleted": True}


@app.get("/api/admin/doctors")
async def admin_doctors(x_admin_token: str = Header(None)):
    """Doctors with their current days/hours, for the schedule editor."""
    _check_admin(x_admin_token)
    return {"doctors": clinic.list_doctors()}


@app.put("/api/admin/doctors/{doctor_id}/schedule")
async def admin_update_schedule(doctor_id: int, request: Request, x_admin_token: str = Header(None)):
    """Change one doctor's working days and hours. Dashboard and bot follow at once."""
    _check_admin(x_admin_token)
    body = await request.json()
    result = clinic.update_doctor_schedule(
        doctor_id,
        days=body.get("days", []),
        start=body.get("start", ""),
        end=body.get("end", ""),
    )
    if not result.get("updated"):
        raise HTTPException(status_code=400, detail=result.get("message", "Invalid schedule"))
    return result


@app.get("/api/admin/calls")
async def admin_calls(x_admin_token: str = Header(None)):
    """Every browser call, newest first, with its transcript length."""
    _check_admin(x_admin_token)
    return {"calls": clinic.list_calls()}


@app.get("/api/admin/calls/{call_id}")
async def admin_call_transcript(call_id: int, x_admin_token: str = Header(None)):
    """The full saved transcript of one call."""
    _check_admin(x_admin_token)
    transcript = clinic.get_call_transcript(call_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return transcript


@app.delete("/api/admin/calls/{call_id}")
async def admin_delete_call(call_id: int, x_admin_token: str = Header(None)):
    """Delete one call and its whole transcript."""
    _check_admin(x_admin_token)
    if not clinic.delete_call(call_id):
        raise HTTPException(status_code=404, detail="Call not found")
    return {"deleted": True}


@app.get("/admin")
async def admin_page():
    return FileResponse(ROOT / "client" / "admin.html")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    events.register(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive; dashboard does not send anything
    except WebSocketDisconnect:
        pass
    finally:
        events.unregister(websocket)


@app.get("/")
async def index():
    return FileResponse(ROOT / "client" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "client"), name="static")


def main():
    missing = [k for k in ("ELEVENLABS_API_KEY", "GOOGLE_API_KEY") if not __import__("os").environ.get(k)]
    if missing:
        print(f"Missing keys in .env: {', '.join(missing)}  (see .env.example)")
        sys.exit(1)
    clinic.init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
