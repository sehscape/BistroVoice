"""Speech in and out, both local and free.

- Speech-to-text: faster-whisper (CTranslate2, int8 on CPU).
- Text-to-speech: Piper (ONNX voices).

Both models are loaded once and reused; each call reports how long it took
so the server and the evaluation can show per-stage latency.
"""

from __future__ import annotations

import io
import math
import os
import re
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import MODELS_DIR, Settings

SAMPLE_RATE = 16_000
PIPER_DIR = MODELS_DIR / "piper"
# Extra voices used only by the evaluation to play the customer's side.
CALLER_VOICES = ["en_US-ryan-medium", "en_GB-northern_english_male-medium"]


def keep_full_cpu_speed() -> None:
    """Opt this process out of Windows 11 power throttling (EcoQoS).

    Windows moves background or minimised processes onto efficiency cores at
    low clock speed. Measured here: Whisper 3-7 s and Piper 2 s per call when
    throttled, ~1 s and ~0.5 s when not. No-op on other systems.
    """
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class _Throttling(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD]
    state = _Throttling(1, 0x1, 0x0)  # execution-speed throttling: off
    kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4,  # ProcessPowerThrottling
                                   ctypes.byref(state), ctypes.sizeof(state))


def download_models(settings: Settings, agent_voice_only: bool = False) -> None:
    from piper.download_voices import download_voice

    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    voices = [settings.tts_voice] if agent_voice_only else [settings.tts_voice, *CALLER_VOICES]
    for voice in voices:
        if not (PIPER_DIR / f"{voice}.onnx").exists():
            print(f"Piper voice {voice} ...")
            download_voice(voice, PIPER_DIR)
    if agent_voice_only:
        return
    from faster_whisper import WhisperModel

    print(f"Whisper model {settings.stt_model} ...")
    WhisperModel(settings.stt_model, device="cpu", compute_type="int8")
    print("Models ready.")


# ====================================================================== speech-to-text


@dataclass
class Transcript:
    text: str
    confidence: float  # 0..1, geometric-mean token probability from Whisper
    no_speech_prob: float
    audio_seconds: float
    ms: float


class Transcriber:
    def __init__(self, model: str = "base.en", hint: str = "", beam_size: int = 5):
        from faster_whisper import WhisperModel

        keep_full_cpu_speed()
        # 8 threads: more only adds contention on laptop efficiency cores.
        threads = min(8, os.cpu_count() or 4)
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
        # Whisper's prompt biases spelling towards words it has "already seen":
        # passing the dish names helps it write "trofie" instead of "trophy".
        self.hint = hint
        self.beam_size = beam_size
        self._lock = threading.Lock()

    def transcribe(self, audio: np.ndarray) -> Transcript:
        """`audio`: mono float32 at 16 kHz."""
        start = time.perf_counter()
        with self._lock:
            segments, _info = self.model.transcribe(
                audio, language="en", beam_size=self.beam_size, temperature=0.0,
                condition_on_previous_text=False, initial_prompt=self.hint or None,
                vad_filter=False, without_timestamps=True,
            )
            segments = list(segments)
        text = " ".join(s.text.strip() for s in segments).strip()
        if segments:
            n = sum(max(1, len(s.tokens)) for s in segments)
            logprob = sum(s.avg_logprob * max(1, len(s.tokens)) for s in segments) / n
            confidence = math.exp(logprob)
            no_speech = max(s.no_speech_prob for s in segments)
        else:
            confidence, no_speech = 0.0, 1.0
        if no_speech > 0.8 and confidence < 0.7:
            text = ""
        return Transcript(text=_clean_transcript(text), confidence=round(confidence, 3),
                          no_speech_prob=round(no_speech, 3),
                          audio_seconds=len(audio) / SAMPLE_RATE,
                          ms=(time.perf_counter() - start) * 1000)


class GroqTranscriber:
    """Same interface, but Whisper runs on Groq's free API.

    Only for the hosted demo: free hosting gives a tenth of a CPU, where local
    Whisper takes ~10 s per sentence. Local runs and the evaluation use
    faster-whisper.
    """

    def __init__(self, api_key: str, base_url: str, hint: str = "",
                 model: str = "whisper-large-v3-turbo"):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=20, max_retries=1)
        self.model = model
        self.hint = hint

    def transcribe(self, audio: np.ndarray) -> Transcript:
        start = time.perf_counter()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        resp = self.client.audio.transcriptions.create(
            model=self.model, file=("speech.wav", buf.getvalue()), language="en",
            prompt=self.hint, response_format="verbose_json", temperature=0.0,
        )
        segments = getattr(resp, "segments", None) or []
        if segments:
            get = (lambda s, k: s[k]) if isinstance(segments[0], dict) else getattr
            logprob = sum(get(s, "avg_logprob") for s in segments) / len(segments)
            no_speech = max(get(s, "no_speech_prob") for s in segments)
            confidence = math.exp(logprob)
        else:
            confidence, no_speech = 0.0, 1.0
        text = (resp.text or "").strip()
        if no_speech > 0.8 and confidence < 0.7:
            text = ""
        return Transcript(text=_clean_transcript(text), confidence=round(confidence, 3),
                          no_speech_prob=round(no_speech, 3), audio_seconds=len(audio) / SAMPLE_RATE,
                          ms=(time.perf_counter() - start) * 1000)


def make_transcriber(settings: Settings, hint: str = ""):
    if settings.stt_backend == "groq":
        return GroqTranscriber(settings.llm_api_key, settings.llm_base_url, hint=hint)
    return Transcriber(settings.stt_model, hint=hint)


def _clean_transcript(text: str) -> str:
    # Whisper sometimes "hears" these on silence or breath noise.
    if re.fullmatch(r"\W*(thank you|thanks for watching|you|bye)\W*", text, re.I):
        return ""
    return text


def menu_hint(conn) -> str:
    dishes = ", ".join(r[0] for r in conn.execute("SELECT dish FROM menu"))
    # The phone example roughly halved phone-number errors in our tests (10/18 -> 13/18 right).
    return (f"A phone call to Olive and Ember restaurant about a table booking. Menu: {dishes}. "
            "Callers give UK phone numbers like 07700 900123 and ask about allergies: "
            "nut free, gluten free, dairy free, shellfish, sesame.")


# ====================================================================== text-to-speech

_SPOKEN_DIGITS = re.compile(r"\b0\d{4}\s?\d{6}\b")


def for_speech(text: str) -> str:
    """Rewrite text so the voice reads it the way a person would say it."""
    t = text
    # phone numbers: read digit by digit, grouped
    t = _SPOKEN_DIGITS.sub(lambda m: _digits(m.group().replace(" ", ""), (5, 3, 3)), t)
    # London landlines: "020 7946 0321" -> "0 2 0, 7 9 4 6, 0 3 2 1"
    t = re.sub(r"\b0\d{2} \d{4} \d{4}\b", lambda m: _digits(m.group().replace(" ", ""), (3, 4, 4)), t)
    # booking references: "reference 1028" -> "reference 1 0 2 8"
    t = re.sub(r"(reference(?: number)?(?: is)?:?\s*)(\d{3,6})\b",
               lambda m: m.group(1) + " ".join(m.group(2)), t, flags=re.I)
    # prices
    t = re.sub(r"£(\d+)\.00\b", r"\1 pounds", t)
    t = re.sub(r"£(\d+)\.(\d{2})\b", lambda m: f"{m.group(1)} pounds {int(m.group(2))}", t)
    # 24-hour times -> 12-hour
    t = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?m)", _twelve_hour, t)
    t = re.sub(r"\b(\d{1,2}):00\s*([ap])\.?m\.?", r"\1 \2m", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", r"\1 \2 \3m", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2})\s*([ap])m\b", lambda m: f"{m.group(1)} {m.group(2).upper()} M", t,
               flags=re.I)
    t = t.replace("&", " and ")
    return re.sub(r"\s+", " ", t).strip()


def _digits(s: str, groups: tuple[int, ...]) -> str:
    out, i = [], 0
    for g in groups:
        out.append(" ".join(s[i:i + g]))
        i += g
    return ", ".join(p for p in out if p)


def _twelve_hour(m: re.Match) -> str:
    raw, h, mm = m.group(1), int(m.group(1)), m.group(2)
    if h < 12 and not raw.startswith("0"):
        # "7:30" is not 07:30: a person saying it means "seven thirty",
        # usually in the evening. Leave it alone rather than add "am".
        return m.group(0)
    suffix = "pm" if h >= 12 else "am"
    h = h % 12 or 12
    return f"{h}{suffix}" if mm == "00" else f"{h}:{mm}{suffix}"


@dataclass
class SpeechChunk:
    text: str
    wav: bytes
    seconds: float
    ms: float


class Speaker:
    def __init__(self, voice: str, length_scale: float = 1.0):
        from piper import PiperVoice
        from piper.config import SynthesisConfig

        keep_full_cpu_speed()
        path = PIPER_DIR / f"{voice}.onnx"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing: run `bistrovoice download-models`")
        threads = int(os.getenv("TTS_THREADS", "0"))
        if not threads:
            self.voice = PiperVoice.load(path)
        else:
            # Small hosts (Render free: 0.1 CPU, 512 MB). ONNX Runtime's default
            # thread pool and spin-waiting fight over the CPU quota (16 s for one
            # sentence), and its memory arena keeps the peak of every sentence
            # (576 MB measured, over the limit). One thread, no spinning, no arena,
            # and only one session loaded.
            import json

            import onnxruntime
            from piper.config import PiperConfig

            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.enable_mem_pattern = False
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            config = json.loads(Path(f"{path}.json").read_text(encoding="utf-8"))
            self.voice = PiperVoice(
                session=onnxruntime.InferenceSession(str(path), sess_options=opts,
                                                     providers=["CPUExecutionProvider"]),
                config=PiperConfig.from_dict(config))
        self.config = SynthesisConfig(length_scale=length_scale)
        # Piper's decoder buffers grow with the length of audio produced in one
        # run; a spoken phone number ("0 7 7 0 0, ...") is long. On a 512 MB host
        # each run is capped at TTS_MAX_CHARS of spoken text (0 = no cap).
        self.max_chars = int(os.getenv("TTS_MAX_CHARS", "0"))
        self.sample_rate = self.voice.config.sample_rate
        self._lock = threading.Lock()

    def sentences(self, text: str) -> list[str]:
        parts = []
        # Synthesis time and memory grow with length, so long sentences are
        # spoken in clause-sized pieces (cut at commas, at most ~80 characters).
        for sentence in (p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p):
            chunk = ""
            for clause in sentence.split(", "):
                joined = f"{chunk}, {clause}" if chunk else clause
                if chunk and len(joined) > 80:
                    parts.append(chunk + ",")
                    chunk = clause
                else:
                    chunk = joined
            parts.append(chunk)
        # A long first piece delays the first sound: speak its opening clause alone.
        if parts and len(parts[0]) > 60:
            head, sep, tail = parts[0].partition(", ")
            if sep and 8 <= len(head) <= 50:
                parts[0:1] = [head + ",", tail]
        return parts

    def synthesize(self, text: str) -> SpeechChunk:
        """One WAV for `text` (usually one sentence)."""
        start = time.perf_counter()
        spoken = for_speech(text)
        pieces = _pieces(spoken, self.max_chars) if self.max_chars else [spoken]
        with self._lock:
            pcm = b"".join(c.audio_int16_bytes for piece in pieces for c in
                           self.voice.synthesize(piece, syn_config=self.config))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)
        return SpeechChunk(text=text, wav=buf.getvalue(), seconds=len(pcm) / 2 / self.sample_rate,
                           ms=(time.perf_counter() - start) * 1000)

    def to_16k(self, wav_bytes: bytes) -> np.ndarray:
        """Decode one of our WAVs to float32 16 kHz (used to feed Whisper in the eval)."""
        with wave.open(io.BytesIO(wav_bytes)) as w:
            rate = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        audio /= 32768.0
        if rate != SAMPLE_RATE:
            n = int(len(audio) * SAMPLE_RATE / rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
        return audio.astype(np.float32)


def _pieces(text: str, limit: int) -> list[str]:
    """Split spoken text into runs of at most `limit` characters, at commas, else spaces."""
    out: list[str] = []
    for clause in re.split(r"(?<=,)\s+", text):
        words, current = clause.split(), ""
        for word in words:
            joined = f"{current} {word}" if current else word
            if current and len(joined) > limit:
                out.append(current)
                current = word
            else:
                current = joined
        if current:
            out.append(current)
    merged: list[str] = []  # put short clauses back together up to the limit
    for p in out:
        if merged and len(merged[-1]) + 1 + len(p) <= limit:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged


def pcm16_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def path_for(voice: str) -> Path:
    return PIPER_DIR / f"{voice}.onnx"
