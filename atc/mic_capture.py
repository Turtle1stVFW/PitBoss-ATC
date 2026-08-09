"""
Microphone capture through the winmm waveIn API.

Whisper wants 16 kHz mono float32, which waveIn can deliver directly, so this
avoids pulling in PortAudio/PyAudio. waveIn always opens shared (it has no
exclusive mode to ask for), so recording here does not disturb SRS on the same
microphone — the one exception being a device where "Allow applications to take
exclusive control" is on and another app has claimed it, which surfaces as an
open failure in last_error rather than as silence.

The device is held open while voice control is enabled and fills a ring buffer.
PTT then just marks offsets, which keeps the first syllable from being clipped
by device-open latency and lets us include a little pre-roll.
"""

from __future__ import annotations

import ctypes
import threading
import time
import winreg
from collections import deque
from ctypes import wintypes

try:
    import numpy as np
except ImportError:  # voice control is optional — the rest of the app still runs
    np = None

SAMPLE_RATE = 16000
CHANNELS = 1
BITS_PER_SAMPLE = 16
CHUNK_MS = 50
BUFFER_COUNT = 8
RING_SECONDS = 30.0
PREROLL_SECONDS = 0.35

WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0x00000000
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0

_MMDEVICE_CAPTURE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
_PROP_DEVICE_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_PROP_FRIENDLY_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"

# waveIn truncates product names to 31 characters plus a terminator.
_WAVEIN_NAME_LIMIT = 31


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_char_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.POINTER(wintypes.DWORD)),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", ctypes.POINTER(wintypes.DWORD)),
]


class WAVEINCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


try:
    _winmm = ctypes.windll.winmm
except (AttributeError, OSError):
    _winmm = None


def supported() -> bool:
    return _winmm is not None and np is not None


def _error_text(code: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    try:
        if _winmm.waveInGetErrorTextW(code, buf, len(buf)) == MMSYSERR_NOERROR:
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return f"waveIn error {code}"


def list_input_devices() -> list[dict[str, object]]:
    """Capture devices as {index, name}. Index -1 is the Windows default."""
    if _winmm is None:
        return []
    devices: list[dict[str, object]] = [{"index": -1, "name": "(Windows default)"}]
    for i in range(int(_winmm.waveInGetNumDevs())):
        caps = WAVEINCAPS()
        if _winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != MMSYSERR_NOERROR:
            continue
        devices.append({"index": i, "name": caps.szPname.strip()})
    return devices


def _endpoint_friendly_name(endpoint_guid: str) -> str:
    """Full 'Headset Microphone (Oculus Virtual Audio Device)' style name."""
    key = f"{_MMDEVICE_CAPTURE}\\{{{endpoint_guid}}}\\Properties"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            def read(name: str) -> str:
                try:
                    return str(winreg.QueryValueEx(handle, name)[0]).strip()
                except OSError:
                    return ""

            pin = read(_PROP_DEVICE_DESC)
            device = read(_PROP_FRIENDLY_NAME)
    except OSError:
        return ""
    if pin and device and pin != device:
        return f"{pin} ({device})"
    return pin or device


def resolve_srs_device(srs_device_id: str) -> tuple[int | None, str]:
    """
    Map an SRS `AudioInputDeviceId` ('{topology}.{endpoint}') to a waveIn index.

    Returns (index or None, friendly name). Matching is by truncated name
    because waveIn and WASAPI do not share identifiers.
    """
    raw = str(srs_device_id or "").strip()
    if "}.{" not in raw:
        return None, ""
    endpoint = raw.split("}.{")[-1].strip("{}")
    name = _endpoint_friendly_name(endpoint)
    if not name:
        return None, ""
    target = name[:_WAVEIN_NAME_LIMIT].casefold()
    for device in list_input_devices():
        index = int(device["index"])  # type: ignore[arg-type]
        if index < 0:
            continue
        if str(device["name"]).casefold().startswith(target):
            return index, name
    return None, name


class MicRecorder:
    """
    Continuously buffers microphone audio; PTT marks a slice to transcribe.

    `start()`/`stop()` open and close the device. `begin_utterance()` marks the
    read point (rewound by PREROLL_SECONDS) and `end_utterance()` returns the
    audio captured since as float32 in [-1, 1].
    """

    def __init__(self, device_index: int = -1) -> None:
        self.device_index = int(device_index)
        self._handle = wintypes.HANDLE()
        self._headers: list[WAVEHDR] = []
        self._buffers: list[ctypes.Array] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ring: deque[np.ndarray] = deque()
        self._ring_samples = 0
        self._max_samples = int(RING_SECONDS * SAMPLE_RATE)
        self._mark: int | None = None
        self._dropped_before_mark = 0
        self._total_captured = 0
        self.last_error = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if _winmm is None or self.running:
            return self.running
        fmt = WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=CHANNELS,
            nSamplesPerSec=SAMPLE_RATE,
            nAvgBytesPerSec=SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8,
            nBlockAlign=CHANNELS * BITS_PER_SAMPLE // 8,
            wBitsPerSample=BITS_PER_SAMPLE,
            cbSize=0,
        )
        device = WAVE_MAPPER if self.device_index < 0 else self.device_index
        rc = _winmm.waveInOpen(
            ctypes.byref(self._handle),
            device,
            ctypes.byref(fmt),
            0,
            0,
            CALLBACK_NULL,
        )
        if rc != MMSYSERR_NOERROR:
            self.last_error = _error_text(rc)
            return False

        chunk_bytes = SAMPLE_RATE * CHUNK_MS // 1000 * (BITS_PER_SAMPLE // 8)
        self._headers = []
        self._buffers = []
        for _ in range(BUFFER_COUNT):
            buf = ctypes.create_string_buffer(chunk_bytes)
            hdr = WAVEHDR(
                lpData=ctypes.cast(buf, ctypes.c_char_p),
                dwBufferLength=chunk_bytes,
                dwBytesRecorded=0,
                dwUser=None,
                dwFlags=0,
                dwLoops=0,
                lpNext=None,
                reserved=None,
            )
            rc = _winmm.waveInPrepareHeader(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
            if rc != MMSYSERR_NOERROR:
                self.last_error = _error_text(rc)
                self._teardown()
                return False
            _winmm.waveInAddBuffer(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._headers.append(hdr)
            self._buffers.append(buf)

        rc = _winmm.waveInStart(self._handle)
        if rc != MMSYSERR_NOERROR:
            self.last_error = _error_text(rc)
            self._teardown()
            return False

        self.last_error = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atc-mic", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
        self._teardown()
        with self._lock:
            self._ring.clear()
            self._ring_samples = 0
            self._mark = None

    def _teardown(self) -> None:
        if not self._handle:
            return
        try:
            _winmm.waveInReset(self._handle)
            for hdr in self._headers:
                _winmm.waveInUnprepareHeader(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
            _winmm.waveInClose(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = wintypes.HANDLE()
        self._headers = []
        self._buffers = []

    def _run(self) -> None:
        # Poll the WHDR_DONE flag rather than using a driver-thread callback,
        # which would mean re-entering Python from an arbitrary thread.
        while not self._stop.is_set():
            progressed = False
            for index, hdr in enumerate(self._headers):
                if not (hdr.dwFlags & WHDR_DONE):
                    continue
                recorded = int(hdr.dwBytesRecorded)
                if recorded:
                    raw = bytes(self._buffers[index].raw[:recorded])
                    samples = np.frombuffer(raw, dtype=np.int16)
                    self._append(samples)
                hdr.dwFlags &= ~WHDR_DONE
                hdr.dwBytesRecorded = 0
                _winmm.waveInAddBuffer(self._handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
                progressed = True
            if not progressed:
                time.sleep(CHUNK_MS / 4000.0)

    def _append(self, samples: np.ndarray) -> None:
        with self._lock:
            self._ring.append(samples)
            self._ring_samples += samples.size
            self._total_captured += samples.size
            while self._ring_samples > self._max_samples:
                oldest = self._ring.popleft()
                self._ring_samples -= oldest.size
                self._dropped_before_mark += oldest.size

    def begin_utterance(self) -> None:
        with self._lock:
            preroll = int(PREROLL_SECONDS * SAMPLE_RATE)
            self._mark = max(0, self._total_captured - preroll)

    def end_utterance(self) -> np.ndarray:
        """Audio since the mark as float32 in [-1, 1]; empty if never marked."""
        with self._lock:
            if self._mark is None or not self._ring:
                self._mark = None
                return np.zeros(0, dtype=np.float32)
            available_start = self._dropped_before_mark
            start = max(self._mark, available_start) - available_start
            data = np.concatenate(list(self._ring)) if len(self._ring) > 1 else self._ring[0]
            self._mark = None
        slice_ = data[start:]
        if slice_.size == 0:
            return np.zeros(0, dtype=np.float32)
        return (slice_.astype(np.float32) / 32768.0).copy()

    def level(self) -> float:
        """Peak amplitude of the most recent chunk, for a UI meter."""
        with self._lock:
            if not self._ring:
                return 0.0
            recent = self._ring[-1]
        if recent.size == 0:
            return 0.0
        return float(np.abs(recent).max()) / 32768.0
