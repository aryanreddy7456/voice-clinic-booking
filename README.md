# City Care Clinic - Voice Appointment Booking

A demo voice AI receptionist for a doctor's clinic, built with [Pipecat](https://github.com/pipecat-ai/pipecat).
You talk to the agent (Aria) from the browser. She can check a doctor's free slots,
book an appointment, and cancel one. All doctors, patients and bookings are fake demo data.

## LLM used

Google **Gemini** (`gemini-flash-lite-latest` by default - the free tier allows many more
requests per day than `gemini-2.5-flash`). Speech-to-text and text-to-speech are
ElevenLabs. Any Gemini model ID can be set in `.env`.

## How it works

```
browser mic -> WebRTC -> ElevenLabs STT -> Gemini (function calling) -> ElevenLabs TTS -> browser speaker
```

- The LLM calls tools registered in the pipeline: `check_doctor_availability`,
  `book_appointment`, `cancel_appointment` (`server/clinic.py`) and `end_call`,
  which hangs up after a short goodbye when the caller is done.
- The caller is identified only by the phone number they speak. The agent reads the
  number back for confirmation before booking or cancelling.
- Doctors, slots and appointments live in a small SQLite file (`data/clinic.db`),
  created automatically on first run. All of it is mock data.
- The dashboard (`client/`) shows doctors and free slots, a live appointments table,
  a live transcript, and a phone-style call screen (timer, mute, end call).

## Setup

1. Install Python 3.10 or newer.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and add your keys:
   - `ELEVENLABS_API_KEY` from https://elevenlabs.io (Profile -> API Keys)
   - `GOOGLE_API_KEY` from https://aistudio.google.com (Get API key)

   Never commit `.env`.

## Run

```bash
python server/main.py
```

Open http://localhost:8000 and press **Try a call**. Allow the microphone when the
browser asks. Speak, for example: "I want to see a dentist this week" - then give
your (fake) phone number when Aria asks.

## Project layout

```
server/main.py    FastAPI app: dashboard, WebRTC offer endpoint, REST + WebSocket APIs
server/bot.py     the Pipecat voice pipeline and the three LLM tools
server/clinic.py  mock clinic data + booking logic (SQLite)
server/events.py  pushes live transcript and booking updates to the dashboard
client/           the dashboard (plain HTML/CSS/JS, no build step)
```

## Admin page (staff only)

Open http://localhost:8000/admin and enter the admin password (`ADMIN_TOKEN` in `.env`, default `clinic-admin-123`).
There you can:
- see every appointment and delete old/completed ones - deleting removes the row, so the time slot becomes free again;
- see a saved transcript of every call (full conversation text with times, one record per "Try a call" session) and delete the ones you do not need;
- change each doctor's working days and hours (e.g. give a doctor evening slots) - the public dashboard and the voice bot use the new timings right away.

This is a simple shared-password check for a local demo, not real login security.

## Notes

- One browser call at a time is expected; each call gets its own pipeline.
- The database is a demo store. Delete `data/clinic.db` to reset all appointments.
