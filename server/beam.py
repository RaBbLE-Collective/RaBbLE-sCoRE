"""
The beam: RaBbLE-Pocket's voice link to sCoRE (beam protocol v0).

Wire format: RaBbLE-Grimoire/RaBbLE-Pocket/RaBbLE-Pocket-Beam-Protocol.md.
One WebSocket per device at /api/v1/beam. A voice turn streams 16 kHz mono
PCM up while the talk button is held; on turn.end sCoRE transcribes it (Groq
Whisper), runs it through RaBbLE (the persona system prompt, a Groq chain
pinned for speed, the device's recent conversation), streams the reply text
back as it's written, and speaks it (Groq Orpheus) in chunks so the first one
plays while the rest is still being written.

Auth: the device sends "Authorization: Bearer <token>". Accepted: the
BEAM_DEVICE_TOKEN env value, a member API key, or a sCoRE JWT. Without one the
beam refuses, even in DEMO_MODE (it spends speech quota); BEAM_ALLOW_GUEST=true
opens it on a trusted LAN for bench testing.

Env:
  GROQ_API_KEY         speech-to-text, text-to-speech and the default chain
  BEAM_DEVICE_TOKEN    the token a Pocket sends (>beam.token= on the device)
  BEAM_ALLOW_GUEST     true: no token needed (local dev only)
  BEAM_LLM_CHAIN       provider:model,... (default Groq gpt-oss-20b, then gpt-oss-120b)
                       a linked, healthy laptop node (server/node.py) is tried first per turn
  BEAM_STT_MODEL       default whisper-large-v3-turbo
  BEAM_TTS_MODEL       default canopylabs/orpheus-v1-english
  BEAM_TTS_VOICE       the voice when the device doesn't pick one (default daniel)
  BEAM_TTS_VOICES      the voices a device may pick per turn (default daniel,austin,troy)
  BEAM_NODE_DEADLINE_S seconds to wait for a linked laptop node before falling back (default 25)
  BEAM_HISTORY_TURNS   conversation turns remembered per device (default 8)
  BEAM_CAPTURE_MAX_S   longest capture recording accepted, seconds (default 600)
"""

import array
import asyncio
import hmac
import io
import json
import logging
import os
import re
import struct
import time
import wave

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import agents
import audit
import auth
import capture
import llm
import node
from tools import server_time

logger = logging.getLogger("rabble.beam")
router = APIRouter()

PROTO = "rabble-beam/0"
KIND_MIC, KIND_REPLY, CODEC_PCM16 = 0x01, 0x02, 0x00
RATE = 16000
FRAME_BYTES = RATE * 2 * 40 // 1000          # 40 ms of reply speech per frame
MAX_NOTE_BYTES = RATE * 2 * 30               # 30 s: the device stops at 15
CAPTURE_MAX_S = int(os.getenv("BEAM_CAPTURE_MAX_S", "600"))
MAX_CAPTURE_BYTES = RATE * 2 * CAPTURE_MAX_S
STT_SEGMENT_BYTES = RATE * 2 * 300           # Whisper gets at most 5 min per request
SHAPE_TIMEOUT_S = 20                         # past this a capture goes out unshaped
KEEP_NOTES, KEEP_NOTES_S = 20, 24 * 3600

GROQ = "https://api.groq.com/openai/v1"
STT_MODEL = os.getenv("BEAM_STT_MODEL", "whisper-large-v3-turbo")
TTS_MODEL = os.getenv("BEAM_TTS_MODEL", "canopylabs/orpheus-v1-english")
TTS_VOICE = os.getenv("BEAM_TTS_VOICE", "daniel")
TTS_VOICES = {v.strip() for v in os.getenv("BEAM_TTS_VOICES", "daniel,austin,troy").split(",") if v.strip()}
TTS_MAX_CHARS = 195                          # Orpheus takes at most 200 per request
TTS_FIRST_MIN = 40                           # the first chunk goes at its first sentence past this
HISTORY_TURNS = int(os.getenv("BEAM_HISTORY_TURNS", "8"))
DEFAULT_CHAIN = "groq:openai/gpt-oss-20b,groq:openai/gpt-oss-120b"
# gpt-oss reasons before it answers; keep that short, it's a conversation.
LLM_EXTRA = {"reasoning_effort": os.getenv("BEAM_REASONING_EFFORT", "low")}

VOICE_PROMPT = """

---
You are speaking aloud, through RaBbLE-Pocket: a small round pendant the human \
wears, with your eyes on its screen. They talk by holding its button; you answer \
in speech.
- Reply in one to three short spoken sentences unless they ask for more.
- Plain speech only: no markdown, lists, code, emoji, URLs or stage directions.
- Write numbers, units and symbols the way they're said.
- What they said came through speech recognition: if it's garbled, say so briefly."""

# Recent conversation per device, so the pendant holds the thread across turns
# and reconnects. In memory: a restart (or Render's cold sleep) forgets it.
_history: dict[str, list[dict]] = {}

# Assembled capture notes per user and device, kept until the device answers
# note.saved, so a note that didn't reach the device is sent again on the next
# connection. Same caveat: in memory only.
_kept: dict[str, list[dict]] = {}


def _keep_note(key: str, note: dict):
    notes = _kept.setdefault(key, [])
    notes[:] = [n for n in notes if n["id"] != note["id"]] + [note]
    _prune_kept(key)


def _prune_kept(key: str):
    notes = [n for n in _kept.get(key, []) if time.time() - n["ts"] < KEEP_NOTES_S]
    notes = notes[-KEEP_NOTES:]
    if notes:
        _kept[key] = notes
    else:
        _kept.pop(key, None)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _authorize(ws: WebSocket) -> dict | None:
    header = ws.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    device_token = os.getenv("BEAM_DEVICE_TOKEN", "").strip()
    if token:
        if device_token and hmac.compare_digest(token.encode(), device_token.encode()):
            return {"sub": "pocket", "name": "RaBbLE-Pocket", "tier": "pro"}
        from auth_routes import verify_api_key  # avoid circular at module load
        user = verify_api_key(token)
        if user:
            return {"sub": user["id"], "name": user["name"], "tier": user.get("tier", "free")}
        try:
            return auth.decode_token(token)
        except Exception:
            return None
    if os.getenv("BEAM_ALLOW_GUEST", "").lower() in ("1", "true", "yes"):
        return {"sub": "guest", "name": "Guest", "tier": "free"}
    return None


# ── Audio ─────────────────────────────────────────────────────────────────────

def _wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _to_pcm16k(wav_bytes: bytes) -> bytes:
    """Any 16-bit PCM WAV to 16 kHz mono 16-bit little-endian, linear interpolation."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"unsupported WAV sample width {width}")
    samples = array.array("h", frames)
    if channels > 1:
        samples = array.array("h", (sum(samples[i:i + channels]) // channels
                                    for i in range(0, len(samples), channels)))
    if rate == RATE:
        return samples.tobytes()
    n_out = int(len(samples) * RATE / rate)
    step = rate / RATE
    out = array.array("h", bytes(2 * n_out))
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = samples[j]
        b = samples[j + 1] if j < last else a
        out[i] = int(a + (b - a) * frac)
    return out.tobytes()


# ── Groq speech ───────────────────────────────────────────────────────────────

class SpeechLimited(Exception):
    """Groq said 429: out of speech quota for now."""


def _groq_headers() -> dict:
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set")
    return {"Authorization": f"Bearer {key}"}


async def transcribe(client: httpx.AsyncClient, pcm: bytes, timeout: float = 30) -> str:
    response = await client.post(
        f"{GROQ}/audio/transcriptions",
        headers=_groq_headers(),
        files={"file": ("note.wav", _wav(pcm), "audio/wav")},
        data={"model": STT_MODEL, "response_format": "json", "temperature": "0"},
        timeout=timeout,
    )
    if response.status_code == 429:
        raise SpeechLimited("speech-to-text")
    if response.status_code != 200:
        raise RuntimeError(f"STT {response.status_code}: {response.text[:200]}")
    return (response.json().get("text") or "").strip()


async def synthesize(client: httpx.AsyncClient, text: str, voice: str = TTS_VOICE) -> bytes:
    response = await client.post(
        f"{GROQ}/audio/speech",
        headers=_groq_headers(),
        json={"model": TTS_MODEL, "voice": voice, "input": text, "response_format": "wav"},
        timeout=30,
    )
    if response.status_code == 429:
        raise SpeechLimited("text-to-speech")
    if response.status_code != 200:
        raise RuntimeError(f"TTS {response.status_code}: {response.text[:200]}")
    return await asyncio.to_thread(_to_pcm16k, response.content)


# ── Speech chunking ───────────────────────────────────────────────────────────

_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s")


def _speakable(text: str) -> str:
    """What the voice should say: markdown and emoji the model slipped in, out."""
    text = re.sub(r"[*_`#>~]|\[(.*?)\]\(.*?\)", lambda m: m.group(1) or "", text)
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Chunker:
    """Cuts streamed reply text into TTS requests: the first as soon as there's
    a sentence (so speech starts early), then as close to the 200-character cap
    as sentences allow (the free plan allows 100 TTS requests a day)."""

    def __init__(self):
        self.buf = ""
        self.first = True

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            cut = self._cut(final=False)
            if cut is None:
                return out
            out.append(cut)

    def flush(self) -> list[str]:
        out = []
        while self.buf.strip():
            out.append(self._cut(final=True))
        return out

    def _cut(self, final: bool) -> str | None:
        ends = [m.end() for m in _SENTENCE_END.finditer(self.buf)]
        fitting = [e for e in ends if e <= TTS_MAX_CHARS]
        at = None
        if self.first:
            enough = [e for e in fitting if e >= TTS_FIRST_MIN]
            at = enough[0] if enough else None
        elif fitting and (final or len(self.buf) > TTS_MAX_CHARS):
            at = fitting[-1]   # as many whole sentences as fit
        if at is None and (final or len(self.buf) > TTS_MAX_CHARS):
            if len(self.buf) <= TTS_MAX_CHARS:
                at = len(self.buf)
            else:   # one long sentence: break at a space
                at = self.buf.rfind(" ", 0, TTS_MAX_CHARS) + 1 or TTS_MAX_CHARS
        if at is None:
            return None
        chunk, self.buf = self.buf[:at], self.buf[at:]
        self.first = False
        return chunk.strip()


# ── A session ─────────────────────────────────────────────────────────────────

class BeamSession:
    def __init__(self, ws: WebSocket, user: dict):
        self.ws = ws
        self.user = user
        self.device = "unknown"
        self.send_lock = asyncio.Lock()
        self.turn: int | None = None
        self.turn_mode = "voice"
        self.speak = True
        self.voice = TTS_VOICE
        self.note_id = None
        self.started = None
        self.tz = None
        self.pcm = bytearray()
        self.task: asyncio.Task | None = None
        self.client = httpx.AsyncClient()
        chain_env = os.getenv("BEAM_LLM_CHAIN", "").strip() or DEFAULT_CHAIN
        self.chain = llm.parse_model_chain(chain_env)

    def turn_chain(self) -> list[dict]:
        """Voice turns: the owner's laptop node first when one is linked and healthy,
        then BEAM_LLM_CHAIN. Capture shaping keeps plain self.chain (spec D4)."""
        return [{**n, "device_id": self.device} for n in node.node_route(self.user["sub"])] + self.chain

    async def send(self, msg: dict):
        async with self.send_lock:
            await self.ws.send_text(json.dumps(msg))

    async def send_audio(self, turn: int, pcm: bytes):
        header = bytes([KIND_REPLY, CODEC_PCM16]) + struct.pack("<H", turn & 0xFFFF)
        # A little ahead of real time: the device buffers ~4 s and pushes back.
        start = time.monotonic()
        sent_s = 0.0
        for i in range(0, len(pcm), FRAME_BYTES):
            async with self.send_lock:
                await self.ws.send_bytes(header + pcm[i:i + FRAME_BYTES])
            sent_s += min(FRAME_BYTES, len(pcm) - i) / (RATE * 2)
            ahead = sent_s - (time.monotonic() - start) * 1.25
            if ahead > 1.0:
                await asyncio.sleep(ahead - 1.0)

    def history_key(self) -> str:
        return f"{self.user['sub']}:{self.device}"

    # ── messages ──

    async def on_text(self, raw: str):
        msg = json.loads(raw)
        kind, turn = msg.get("type"), msg.get("turn")
        if kind == "hello":
            self.device = str(msg.get("device", "unknown"))[:40]
            logger.info("beam hello device=%s fw=%s proto=%s user=%s",
                        self.device, msg.get("fw"), msg.get("proto"), self.user["sub"])
            # time: UTC seconds (tools.server_time), so the device sets its clock on every
            # connect; it adds half the round trip.
            await self.send({"type": "ready", "name": "sCoRE", "time": server_time()["time"]})
            await self.resend_kept()
        elif kind == "turn.begin":
            self.cancel_task()
            self.turn, self.turn_mode = turn, msg.get("mode", "voice")
            self.speak = bool(msg.get("speak", True)) and self.turn_mode == "voice"
            voice = str(msg.get("voice", "")).lower()
            self.voice = voice if voice in TTS_VOICES else TTS_VOICE
            self.pcm = bytearray()
            if self.turn_mode == "capture":
                self.speak = False
                self.note_id, self.started, self.tz = msg.get("id"), msg.get("started"), msg.get("tz")
                if msg.get("text") is not None:   # typed capture: no speech to wait for
                    await self.send({"type": "sent", "turn": turn})
                    self.task = asyncio.create_task(self.run_capture(turn, text=str(msg["text"])))
            elif self.turn_mode == "text":
                text = str(msg.get("text", ""))
                await self.send({"type": "sent", "turn": turn})
                self.task = asyncio.create_task(self.run_turn(turn, text=text))
        elif kind == "turn.end" and turn == self.turn and self.turn_mode in ("voice", "capture"):
            pcm, self.pcm = bytes(self.pcm), bytearray()
            await self.send({"type": "sent", "turn": turn})
            if self.turn_mode == "capture":
                self.task = asyncio.create_task(self.run_capture(turn, pcm=pcm))
            else:
                self.task = asyncio.create_task(self.run_turn(turn, pcm=pcm))
        elif kind == "note.saved":
            key = self.history_key()
            left = [n for n in _kept.get(key, []) if n["id"] != msg.get("id")]
            if left:
                _kept[key] = left
            else:
                _kept.pop(key, None)
        elif kind == "turn.cancel":
            if turn == self.turn:
                self.cancel_task()
                self.turn = None

    def on_audio(self, data: bytes):
        if len(data) < 4 or data[0] != KIND_MIC or data[1] != CODEC_PCM16 or self.turn is None:
            return
        if struct.unpack("<H", data[2:4])[0] != self.turn & 0xFFFF:
            return
        cap = MAX_CAPTURE_BYTES if self.turn_mode == "capture" else MAX_NOTE_BYTES
        if len(self.pcm) < cap:
            self.pcm += data[4:4 + cap - len(self.pcm)]

    def cancel_task(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None

    # ── a turn ──

    async def run_turn(self, turn: int, pcm: bytes | None = None, text: str | None = None):
        t0 = time.monotonic()
        marks = {}
        try:
            if pcm is not None:
                secs = len(pcm) / (RATE * 2)
                if secs < 0.3:
                    await self.send({"type": "error", "turn": turn, "text": "TOO SHORT TO HEAR"})
                    return
                text = await transcribe(self.client, pcm)
                marks["stt"] = time.monotonic() - t0
                logger.info("beam heard %.1fs -> %r (%.2fs)", secs, text, marks["stt"])
                if not text:
                    await self.send({"type": "error", "turn": turn, "text": "DIDN'T CATCH THAT"})
                    return
                await self.send({"type": "heard", "turn": turn, "text": text, "final": True})

            history = _history.setdefault(self.history_key(), [])
            messages = history + [{"role": "user", "content": text}]
            system = agents.rabble_system_prompt("default", "fast") + VOICE_PROMPT

            # Speech runs beside the writing: a queue of chunks, said in order.
            speech: asyncio.Queue[str | None] = asyncio.Queue()
            speaker = asyncio.create_task(self.speak_chunks(turn, speech, marks, t0)) if self.speak else None
            chunker = Chunker()
            reply = []
            async for piece in llm.stream_chat(messages, system, model_tier="fast",
                                               chain=self.turn_chain(), user=self.user, max_tokens=600,
                                               extra_body=LLM_EXTRA):
                if "first_text" not in marks:
                    marks["first_text"] = time.monotonic() - t0
                reply.append(piece)
                await self.send({"type": "reply", "turn": turn, "text": piece})
                if speaker:
                    for chunk in chunker.feed(piece):
                        await speech.put(chunk)
            if speaker:
                for chunk in chunker.flush():
                    await speech.put(chunk)
                await speech.put(None)
                await speaker

            full = "".join(reply).strip()
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": full}])
            del history[:-2 * HISTORY_TURNS]
            await self.send({"type": "done", "turn": turn})
            marks["done"] = time.monotonic() - t0
            logger.info("beam turn %s done: %s", turn, " ".join(f"{k}={v:.2f}s" for k, v in marks.items()))
            audit.log(user_id=self.user["sub"], action="beam", tier="fast", workflow_type="voice", status=200)
        except asyncio.CancelledError:
            raise
        except SpeechLimited as exc:
            logger.warning("beam %s rate-limited", exc)
            await self.send({"type": "error", "turn": turn, "text": "SPEECH LIMIT REACHED"})
        except Exception as exc:
            logger.exception("beam turn %s failed", turn)
            audit.log(user_id=self.user["sub"], action="beam", tier="fast", workflow_type="voice",
                      status=500, error=str(exc))
            try:
                await self.send({"type": "error", "turn": turn, "text": "sCoRE HIT A SNAG"})
            except Exception:
                pass

    # ── a capture ──

    async def run_capture(self, turn: int, pcm: bytes | None = None, text: str | None = None):
        """A note, not a conversation: transcribe, shape, send the file. No reply,
        no speech, and the conversation history is never touched."""
        t0 = time.monotonic()
        try:
            source, duration = "text", None
            if pcm is not None:
                source, duration = "voice", round(len(pcm) / (RATE * 2))
                if len(pcm) / (RATE * 2) < 0.3:
                    await self.send({"type": "error", "turn": turn, "text": "TOO SHORT TO HEAR"})
                    return
                # Whisper takes long audio badly (and a body limit): 5 min at a time.
                texts = [await transcribe(self.client, pcm[i:i + STT_SEGMENT_BYTES], timeout=120)
                         for i in range(0, len(pcm), STT_SEGMENT_BYTES)]
                text = " ".join(t for t in texts if t)
            text = (text or "").strip()
            if not text:
                await self.send({"type": "error", "turn": turn, "text": "DIDN'T CATCH THAT"})
                return
            if source == "voice":
                await self.send({"type": "heard", "turn": turn, "text": text, "final": True})

            shaped = await self.shape(text, source)
            created = capture.local_time(self.started, self.tz)
            note_id = capture.safe_id(self.note_id, self.started or time.time(), self.tz, turn)
            md = capture.assemble_note(note_id, created, self.device, source, duration, text, shaped)
            note = {"id": note_id, "title": capture.note_title(md), "md": md,
                    "path": capture.note_path(note_id, created), "ts": time.time()}
            _keep_note(self.history_key(), note)   # kept first: the link may drop mid-send
            await self.send_note(turn, note)
            await self.send({"type": "done", "turn": turn})
            logger.info("beam capture %s done: %s %.2fs shaped=%s", note_id, source,
                        time.monotonic() - t0, bool(shaped))
            audit.log(user_id=self.user["sub"], action="beam", tier="fast", workflow_type="capture", status=200)
        except asyncio.CancelledError:
            raise
        except SpeechLimited as exc:
            logger.warning("beam %s rate-limited", exc)
            await self.send({"type": "error", "turn": turn, "text": "SPEECH LIMIT REACHED"})
        except Exception as exc:
            logger.exception("beam capture %s failed", turn)
            audit.log(user_id=self.user["sub"], action="beam", tier="fast", workflow_type="capture",
                      status=500, error=str(exc))
            try:
                await self.send({"type": "error", "turn": turn, "text": "sCoRE HIT A SNAG"})
            except Exception:
                pass

    async def shape(self, transcript: str, source: str) -> dict | None:
        """One fast-tier call for title, tags, summary and notes; None on any
        failure, so the note still goes out with the transcript alone."""
        system, messages = capture.build_prompt(transcript, source)

        async def collect() -> str:
            return "".join([p async for p in llm.stream_chat(
                messages, system, model_tier="fast", chain=self.chain,
                user=self.user, max_tokens=2000, extra_body=LLM_EXTRA)])
        try:
            return capture.parse_shaped(await asyncio.wait_for(collect(), SHAPE_TIMEOUT_S))
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # includes the timeout
            logger.warning("beam capture shaping failed: %r", exc)
            return None

    async def send_note(self, turn: int, note: dict):
        parts = capture.split_parts(note["md"])
        for seq, part in enumerate(parts):
            await self.send({"type": "note", "turn": turn, "id": note["id"], "title": note["title"],
                             "path": note["path"], "seq": seq, "last": seq == len(parts) - 1, "md": part})

    async def resend_kept(self):
        key = self.history_key()
        _prune_kept(key)
        for note in list(_kept.get(key, [])):
            await self.send_note(0, note)

    async def speak_chunks(self, turn: int, speech: asyncio.Queue, marks: dict, t0: float):
        """Synthesizes each chunk while the one before plays; out of quota, the
        rest of the reply stays text."""
        pending: asyncio.Task | None = None
        quiet = False
        while True:
            chunk = await speech.get()
            nxt = None
            if chunk and not quiet:
                text = _speakable(chunk)
                if text:
                    nxt = asyncio.create_task(synthesize(self.client, text, self.voice))
            if pending:
                try:
                    pcm = await pending
                    if "first_audio" not in marks:
                        marks["first_audio"] = time.monotonic() - t0
                    await self.send_audio(turn, pcm)
                except SpeechLimited:
                    logger.warning("beam TTS rate-limited: rest of the reply is text only")
                    quiet = True
                    if nxt:
                        nxt.cancel()
                        nxt = None
                except Exception as exc:
                    logger.warning("beam TTS failed: %s", exc)
            pending = nxt
            if chunk is None:
                return

    async def close(self):
        self.cancel_task()
        await self.client.aclose()


@router.websocket("/api/v1/beam")
async def beam(ws: WebSocket):
    user = _authorize(ws)
    if not user:
        logger.warning("beam refused: no valid token from %s", ws.client.host if ws.client else "?")
        await ws.close(code=4401, reason="device token required")
        return
    await ws.accept()
    session = BeamSession(ws, user)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                session.on_audio(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    await session.on_text(msg["text"])
                except (ValueError, KeyError) as exc:
                    logger.warning("beam bad message: %s", exc)
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        logger.info("beam closed device=%s", session.device)
