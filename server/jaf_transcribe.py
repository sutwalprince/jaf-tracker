#!/usr/bin/env python3
"""Transcribes a WhatsApp voice note on this server (nothing is sent anywhere) and prints the text.

  jaf_transcribe.py <audio file> [hint words]

Uses faster-whisper's multilingual "base" model (English, Hindi, Hinglish), int8 on the CPU. It runs as a
short-lived process, so the model's ~300 MB is freed as soon as it's done. The first run downloads the model
(~145 MB) into ~/jaf-sync/whisper-models. Hint words (e.g. your company names) improve spelling of names.
"""
import sys
from pathlib import Path

import av
import numpy as np
from faster_whisper import WhisperModel

MODELS = Path.home() / "jaf-sync" / "whisper-models"
RATE = 16000


def load_audio(path):
    """Decodes any voice-note format to 16 kHz mono float32 (as Whisper expects), with the installed PyAV."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    audio = load_audio(sys.argv[1])
    model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2, download_root=str(MODELS))
    segments, _info = model.transcribe(audio, beam_size=1, vad_filter=True,
                                       initial_prompt=sys.argv[2] if len(sys.argv) > 2 else None)
    print(" ".join(s.text.strip() for s in segments).strip())


if __name__ == "__main__":
    main()
