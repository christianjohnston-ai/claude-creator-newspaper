#!/usr/bin/env python3
"""Transcribe one audio file on this computer and write a JSON transcript.

watch.py runs this file with whichever Python has the speech-to-text package
installed (often the skill's own .venv), so the main scripts stay standard library.

  python transcribe_one.py AUDIO.wav OUT.json [--engine auto|mlx|faster] [--model NAME]

Engines:
  mlx     mlx-whisper, for Apple Silicon Macs (pip install mlx-whisper)
  faster  faster-whisper, for everything else (pip install faster-whisper)
Exit code 4 means no engine is installed in this Python.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys

MLX_DEFAULT = "mlx-community/whisper-large-v3-turbo"
FASTER_DEFAULT = "small"


def have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def pick_engine(engine: str) -> str:
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    if engine == "auto":
        if apple_silicon and have("mlx_whisper"):
            return "mlx"
        if have("faster_whisper"):
            return "faster"
        if have("mlx_whisper"):
            return "mlx"
        return ""
    if engine == "mlx" and have("mlx_whisper"):
        return "mlx"
    if engine == "faster" and have("faster_whisper"):
        return "faster"
    return ""


def run_mlx(audio: str, model: str):
    import mlx_whisper
    res = mlx_whisper.transcribe(audio, path_or_hf_repo=model or MLX_DEFAULT,
                                 condition_on_previous_text=False, verbose=None)
    segs = [{"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2),
             "text": s["text"].strip(), "no_speech_prob": round(float(s.get("no_speech_prob", 0.0)), 3)}
            for s in res.get("segments", [])]
    return model or MLX_DEFAULT, res.get("language"), segs


def run_faster(audio: str, model: str):
    from faster_whisper import WhisperModel
    m = WhisperModel(model or FASTER_DEFAULT, device="auto", compute_type="int8")
    seg_iter, info = m.transcribe(audio, vad_filter=True, condition_on_previous_text=False)
    segs = [{"start": round(float(s.start), 2), "end": round(float(s.end), 2), "text": s.text.strip(),
             "no_speech_prob": round(float(getattr(s, "no_speech_prob", 0.0)), 3)} for s in seg_iter]
    return model or FASTER_DEFAULT, getattr(info, "language", None), segs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--engine", default="auto", choices=("auto", "mlx", "faster"))
    ap.add_argument("--model", default="")
    ap.add_argument("--check", action="store_true", help="only report which engine is available")
    a = ap.parse_args()
    eng = pick_engine(a.engine)
    if a.check:
        print(eng or "none")
        return 0 if eng else 4
    if not eng:
        print("No speech-to-text engine in this Python (%s). Install mlx-whisper (Apple Silicon) "
              "or faster-whisper." % sys.executable, file=sys.stderr)
        return 4
    model, lang, segs = run_mlx(a.audio, a.model) if eng == "mlx" else run_faster(a.audio, a.model)
    # Segments Whisper itself marks as probably silence are dropped (they are where it invents words).
    kept = [s for s in segs if s["text"] and s["no_speech_prob"] < 0.8]
    text = " ".join(s["text"] for s in kept).strip()
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"engine": "mlx-whisper" if eng == "mlx" else "faster-whisper", "model": model,
                   "language": lang, "text": text, "segments": kept}, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
