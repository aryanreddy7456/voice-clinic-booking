"""The voice agent.

One Pipecat pipeline per browser call:

    caller audio -> STT (ElevenLabs) -> LLM (Gemini) -> TTS (ElevenLabs) -> caller audio

The LLM can call four tools: check_doctor_availability, book_appointment and
cancel_appointment live in clinic.py; end_call hangs up after the goodbye
(with a transcript-watching safety net in case the LLM skips the tool call).
The caller is identified only by the phone number they say out loud.
"""

from __future__ import annotations

import asyncio
import os

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import LLMUserAggregatorParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext, OpenAILLMContextFrame
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.elevenlabs.stt import CommitStrategy, ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

import clinic
import events

# Printed at server start and at the top of every call log, so anyone running
# this file can confirm which version they actually have. Bump on every change.
BOT_VERSION = "1.6"
logger.info(f"bot.py loaded - version {BOT_VERSION}")

class LLMErrorRetry(FrameProcessor):
    """Retries the LLM turn when the model API returns an error.

    The free Gemini endpoint occasionally rejects a turn mid-call (for example
    with a 400 about thought signatures). Without this, the caller would just
    hear silence. On an error we re-run the last caller message, up to
    max_attempts times; the counter resets on every good reply.

    We only retry if the turn has not spoken yet. Some errors (like a context
    bookkeeping error in the framework) arrive AFTER the reply already played
    out - retrying those would make the agent say everything twice.
    """

    def __init__(self, retry, max_attempts: int = 2, on_error=None):
        super().__init__()
        self._retry = retry
        self._max_attempts = max_attempts
        self._attempts = 0
        self._turn_spoke = False
        self._on_error = on_error  # optional: called with a short note for the saved transcript

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._turn_spoke = False
        elif isinstance(frame, LLMTextFrame):
            self._turn_spoke = True
        elif isinstance(frame, ErrorFrame):
            if self._turn_spoke:
                logger.warning("LLM error after the reply already played; not retrying")
                if self._on_error is not None:
                    self._on_error("(a technical error occurred after this reply)")
            elif self._attempts < self._max_attempts:
                self._attempts += 1
                logger.warning(f"LLM error before any reply, retrying the turn (attempt {self._attempts})")
                if self._on_error is not None:
                    self._on_error(f"(the assistant hit a technical error and retried, attempt {self._attempts})")
                await asyncio.sleep(1)
                self._retry()
            else:
                logger.error("LLM kept failing; giving up on this turn")
                if self._on_error is not None:
                    self._on_error("(the assistant could not answer: the AI service kept failing)")
            return  # swallow the error so the pipeline keeps running
        if isinstance(frame, LLMFullResponseEndFrame):
            self._attempts = 0
        await self.push_frame(frame, direction)


class ToolHistoryScrubber(FrameProcessor):
    """Rewrites tool calls in the LLM context as plain text before each run.

    Gemini's newer models reject function-call history replayed without its
    internal "thought signature" (a 400 INVALID_ARGUMENT error), which makes
    the agent go silent mid-call. Pipecat 0.0.108 does not preserve those
    signatures, so instead we never let old tool traffic reach the API: every
    past function call/result becomes a short text note, which carries the
    same information and never triggers the signature check.
    """

    @staticmethod
    def _fn_parts(message):
        """Return (function_call, function_response) of a genai Content, if any."""
        fc = fr = None
        for part in getattr(message, "parts", None) or []:
            if getattr(part, "function_call", None):
                fc = part.function_call
            if getattr(part, "function_response", None):
                fr = part.function_response
        return fc, fr

    @staticmethod
    def _scrub(messages):
        try:
            from google.genai.types import Content, Part
        except Exception:
            Content = None

        def is_tool_msg(m):
            if isinstance(m, dict):
                return m.get("role") == "tool" or m.get("tool_calls")
            fc, fr = ToolHistoryScrubber._fn_parts(m)
            return bool(fc or fr)

        out = []
        changed = False
        for m in messages:
            if isinstance(m, dict):
                if m.get("tool_calls"):
                    names = ", ".join(tc.get("function", {}).get("name", "?") for tc in m["tool_calls"])
                    out.append({"role": "assistant", "content": f"(I used the tool: {names}.)"})
                    changed = True
                elif m.get("role") == "tool":
                    out.append({"role": "user", "content": f"Tool result: {m.get('content', '')} Tell the caller the result."})
                    changed = True
                else:
                    out.append(m)
                continue
            fc, fr = ToolHistoryScrubber._fn_parts(m)
            if fc and Content is not None:
                out.append(Content(role="model", parts=[Part(text=f"(I used the tool: {fc.name}.)")]))
                changed = True
            elif fr and Content is not None:
                out.append(Content(role="user", parts=[Part(text=f"Tool {fr.name} returned this result: {fr.response}. Tell the caller the result.")]))
                changed = True
            else:
                out.append(m)
        if changed:
            logger.debug("Scrubbed tool history into plain text for the LLM request")
            messages[:] = out

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (OpenAILLMContextFrame, LLMContextFrame)) and hasattr(frame.context, "messages"):
            self._scrub(frame.context.messages)
        await self.push_frame(frame, direction)


# Phrases that show the caller is done, and phrases that show the agent said goodbye.
DONE_WORDS = ("thank", "bye", "nothing else", "that's all", "that is all", "no more", "all i needed")
GOODBYE_WORDS = ("goodbye", "bye", "take care", "have a great", "have a wonderful", "talk to you soon")


class CallState:
    """Shared per-call state the transcript taps use to spot the goodbye moment."""

    def __init__(self):
        self.last_user_text = ""
        self.hangup_started = False


class TranscriptTap(FrameProcessor):
    """Watches frames going past and reports finished utterances to the dashboard.

    role="assistant": sits right after the LLM and collects its streamed text
    until the response ends. role="user": sits right after the STT and reports
    each final transcription.

    When the caller's last message sounded done (thanks/bye) and the agent's
    reply sounded like a goodbye, it fires on_goodbye. This is the safety net
    that hangs up even if the LLM forgets to call the end_call tool.
    """

    def __init__(self, role: str, call_state: CallState | None = None, on_goodbye=None, on_line=None):
        super().__init__()
        self._role = role
        self._parts: list[str] = []
        self._call_state = call_state
        self._on_goodbye = on_goodbye
        self._on_line = on_line  # optional: called with (role, text) to save the transcript

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._role == "assistant":
            if isinstance(frame, LLMTextFrame):
                self._parts.append(frame.text)
            elif isinstance(frame, ErrorFrame):
                # A turn that errored midway: keep whatever was said in the transcript.
                if self._parts:
                    text = "".join(self._parts).strip()
                    self._parts = []
                    if text:
                        events.publish({"type": "transcript", "role": "assistant", "content": text})
                        if self._on_line is not None:
                            self._on_line("assistant", text)
            elif isinstance(frame, LLMFullResponseEndFrame) and self._parts:
                text = "".join(self._parts).strip()
                self._parts = []
                if text:
                    logger.info(f"Transcript [assistant]: {text}")
                    events.publish({"type": "transcript", "role": "assistant", "content": text})
                    if self._on_line is not None:
                        self._on_line("assistant", text)
                    state = self._call_state
                    if state is not None and self._on_goodbye is not None and not state.hangup_started:
                        agent_said_bye = any(w in text.lower() for w in GOODBYE_WORDS)
                        caller_was_done = any(w in state.last_user_text for w in DONE_WORDS)
                        if agent_said_bye and caller_was_done:
                            logger.info("Caller said goodbye; hanging up")
                            self._on_goodbye()
        else:
            if isinstance(frame, TranscriptionFrame):
                text = frame.text.strip()
                if text:
                    logger.info(f"Transcript [user]: {text}")
                    events.publish({"type": "transcript", "role": "user", "content": text})
                    if self._on_line is not None:
                        self._on_line("user", text)
                    if self._call_state is not None:
                        self._call_state.last_user_text = text.lower()
        await self.push_frame(frame, direction)


SYSTEM_PROMPT = """You are Aria, the friendly phone receptionist of City Care Clinic, a demo clinic.

You can do exactly three things using your tools:
1. check_doctor_availability - find free slots for a doctor (or a speciality, like "dentist") and/or a date.
2. book_appointment - book a slot for the caller.
3. cancel_appointment - cancel the caller's appointment.

Rules you must follow:
- The caller is identified ONLY by the phone number they say out loud. There is no login.
- Phone numbers often arrive in PIECES across several messages (listening can cut in mid-number).
  Collect the digits the caller says, combine them in order, and only read the number back when
  you have the whole number (Indian mobile numbers are usually 10 digits). If the caller gives
  only part of the number, ask for the remaining digits and combine them with what you already
  heard. If the caller corrects a digit, use the corrected version. Never treat a short fragment
  as a complete number.
- Before you book or cancel, read the full number back digit by digit and wait for the caller
  to confirm it is correct. Only then call the tool.
- Today is {today}. When the caller says "tomorrow" or a weekday, turn it into a real date (YYYY-MM-DD).
- Keep answers short and natural: this is a phone call, not an email. Speak one or two sentences at a time.
- When listing slots, offer at most three options at a time.
- Never invent doctors, slots, or appointments. Only use what the tools return.
- This is a demo: all doctors, patients and bookings are fake.
- When the caller asks for a specific time, do not guess whether it is free: call
  book_appointment with it. The tool itself rejects times that are not open slots and
  returns the real free slots. If it rejects, say that time is not available and read
  out the real free slots from the tool's answer, then let the caller pick one.
- If the caller asks for a loose time like "around 1:45" or "some time after 2", first
  check availability for that day, then offer the free slot(s) closest to what they
  asked for. Never book a time that is not one of the exact free slots.
- If the caller asks for a time when the doctor simply has no slot (like 5 PM when the
  doctor finishes at 2 PM), politely say that time is not available and that those are
  the only slots, then read out the real free slots. Never book outside them.
- After book_appointment returns booked=true, ALWAYS say the confirmation out loud in
  one short sentence with the doctor's name, the date and the time. If the tool returns
  booked=false, never say it worked - explain what went wrong and offer the real options.
- When the caller is done - they say thanks, thank you, bye, or that they need nothing more,
  especially right after a booking or cancellation - say ONE short goodbye sentence and then
  call the end_call tool IN THE SAME TURN. You MUST call end_call after every goodbye;
  never end your turn with a goodbye unless you also call end_call.
- Never end the call while the caller still has an open request.

Start by greeting the caller briefly and asking how you can help.
"""


def build_tools() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="check_doctor_availability",
                description="Check free appointment slots for a doctor (by name or speciality) and/or a date.",
                properties={
                    "doctor_name": {
                        "type": "string",
                        "description": "Part of the doctor's name or speciality, e.g. 'Sharma' or 'dentist'. Optional.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date as YYYY-MM-DD. Optional; omit to search the next days.",
                    },
                },
                required=[],
            ),
            FunctionSchema(
                name="book_appointment",
                description="Book an appointment slot for the caller. Only call after the caller confirmed their phone number.",
                properties={
                    "phone": {"type": "string", "description": "The caller's phone number as they spoke it."},
                    "doctor_name": {"type": "string", "description": "Doctor name or speciality."},
                    "date": {"type": "string", "description": "YYYY-MM-DD."},
                    "time": {"type": "string", "description": "HH:MM, 24-hour."},
                    "patient_name": {"type": "string", "description": "Caller's name if they gave one. Optional."},
                },
                required=["phone", "doctor_name", "date", "time"],
            ),
            FunctionSchema(
                name="cancel_appointment",
                description="Cancel the caller's existing appointment, found by their phone number. Only call after the caller confirmed their phone number.",
                properties={
                    "phone": {"type": "string", "description": "The caller's phone number as they spoke it."},
                },
                required=["phone"],
            ),
            FunctionSchema(
                name="end_call",
                description="End the phone call. Only call this after saying a short goodbye, when the caller has said thanks, bye, or that they need nothing more.",
                properties={},
                required=[],
            ),
        ]
    )


async def run_bot(webrtc_connection: SmallWebRTCConnection) -> None:
    """Run one voice session for one browser connection."""
    from datetime import datetime

    logger.info(f"Starting voice session (bot.py version {BOT_VERSION})")
    # Open a transcript record for this call; every spoken line is saved to it.
    call_id = clinic.start_call()

    def record_line(role: str, text: str):
        try:
            clinic.add_transcript_line(call_id, role, text)
        except Exception as e:  # never break a call because of transcript saving
            logger.warning(f"Could not save transcript line: {e}")

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            # Give the caller a longer pause before we treat them as done speaking,
            # so digit strings are not cut in half.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.9)),
        ),
    )

    stt = ElevenLabsRealtimeSTTService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        commit_strategy=CommitStrategy.VAD,
    )

    llm = GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        model=os.environ.get("GOOGLE_MODEL", "gemini-flash-lite-latest"),
    )

    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        voice_id=os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),  # Rachel
        model=os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
    )

    call_state = CallState()

    def schedule_hangup():
        """Hang up once, a few seconds after the goodbye so it finishes playing."""
        if call_state.hangup_started:
            return
        call_state.hangup_started = True

        async def hang_up():
            await asyncio.sleep(4)
            logger.info("Ending the call after goodbye")
            events.publish({"type": "call_state", "state": "ended"})
            try:
                await task.queue_frames([EndFrame()])
                await asyncio.sleep(1.5)  # let the pipeline flush
            finally:
                await webrtc_connection.disconnect()

        asyncio.get_running_loop().create_task(hang_up())

    # --- tool handlers: thin wrappers over clinic.py that also update the dashboard ---
    async def check_doctor_availability(params: FunctionCallParams):
        args = params.arguments or {}
        result = clinic.check_doctor_availability(
            doctor_name=args.get("doctor_name"), date=args.get("date")
        )
        await params.result_callback(result)

    async def book_appointment(params: FunctionCallParams):
        args = params.arguments or {}
        result = clinic.book_appointment(
            phone=args.get("phone", ""),
            doctor_name=args.get("doctor_name", ""),
            date=args.get("date", ""),
            time=args.get("time", ""),
            patient_name=args.get("patient_name"),
        )
        if result.get("booked"):
            events.publish({"type": "appointments", "appointments": clinic.list_appointments(), "changed": "booked"})
        await params.result_callback(result)

    async def cancel_appointment(params: FunctionCallParams):
        args = params.arguments or {}
        result = clinic.cancel_appointment(phone=args.get("phone", ""))
        if result.get("cancelled"):
            events.publish({"type": "appointments", "appointments": clinic.list_appointments(), "changed": "cancelled"})
        await params.result_callback(result)

    async def end_call(params: FunctionCallParams):
        await params.result_callback({"ended": True, "message": "The call has ended. Do not say anything else."})
        schedule_hangup()

    llm.register_function("check_doctor_availability", check_doctor_availability)
    llm.register_function("book_appointment", book_appointment)
    llm.register_function("cancel_appointment", cancel_appointment)
    llm.register_function("end_call", end_call)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=datetime.now().strftime("%Y-%m-%d, %A"))}
    ]
    context = OpenAILLMContext(messages=messages, tools=build_tools())
    # Wait a little longer for one caller message to fully arrive before answering.
    context_aggregator = llm.create_context_aggregator(
        context, user_params=LLMUserAggregatorParams(aggregation_timeout=1.0)
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            TranscriptTap("user", call_state=call_state, on_line=record_line),
            context_aggregator.user(),
            ToolHistoryScrubber(),
            llm,
            TranscriptTap("assistant", call_state=call_state, on_goodbye=schedule_hangup, on_line=record_line),
            LLMErrorRetry(
                retry=lambda: asyncio.get_running_loop().create_task(task.queue_frames([LLMRunFrame()])),
                on_error=lambda note: record_line("error", note),
            ),
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Caller connected")
        events.publish({"type": "call_state", "state": "connected"})
        # Nudge the LLM to speak first (the greeting).
        messages.append({"role": "system", "content": "Please greet the caller now, briefly."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Caller disconnected")
        clinic.finish_call(call_id)
        events.publish({"type": "call_state", "state": "ended"})
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
