#!/usr/bin/env python3
"""Transcrição no PC (faster-whisper em CUDA) com a saída no mesmo formato do mlx_whisper.transcribe.

Roda no venv %LOCALAPPDATA%\\Ponte\\venvs\\whisper. Uso:
    python whisper_ponte.py <audio> <saida.json> [--modelo large-v3-turbo] [--idioma pt] [--opcoes '{"...": ...}']
"""
import argparse
import json
import os
import site
import sys
import time
from pathlib import Path


def preparar_cuda():
    """As DLLs de cuBLAS e cuDNN vêm dos pacotes nvidia-* do pip; o Windows precisa achá-las."""
    for base in site.getsitepackages():
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            d = Path(base) / sub
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


MODELOS = {}
TRAVA = None


def carregar(nome):
    from faster_whisper import WhisperModel
    if nome not in MODELOS:
        MODELOS[nome] = WhisperModel(nome, device="cuda", compute_type="float16")
    return MODELOS[nome]


def decodificar(entrada):
    """Arquivo ou bytes -> 16 kHz mono float32, pelo FFmpeg (o PyAV novo quebrou o decodificador do faster-whisper)."""
    import subprocess
    import numpy as np
    if isinstance(entrada, (bytes, bytearray)):
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                           input=entrada, capture_output=True, check=True)
    else:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", entrada, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                           capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.float32)


def transcrever(audio, modelo="large-v3-turbo", idioma="pt", beam=None, op=None):
    op = op or {}
    segs, info = carregar(modelo).transcribe(
        audio,
        language=idioma,
        word_timestamps=op.get("word_timestamps", True),
        condition_on_previous_text=op.get("condition_on_previous_text", False),
        temperature=op.get("temperature", 0.0),
        initial_prompt=op.get("initial_prompt"),
        beam_size=beam or op.get("beam_size", 5),
        vad_filter=False,  # o mlx_whisper não filtra; os tempos precisam bater com o áudio inteiro
    )
    saida = {"text": "", "language": info.language, "segments": []}
    for i, s in enumerate(segs):
        saida["segments"].append({
            "id": i, "seek": 0, "start": s.start, "end": s.end, "text": s.text,
            "tokens": list(s.tokens), "temperature": s.temperature, "avg_logprob": s.avg_logprob,
            "compression_ratio": s.compression_ratio, "no_speech_prob": s.no_speech_prob,
            "words": [{"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                      for w in (s.words or [])],
        })
        saida["text"] += s.text
    return saida, info.duration


def servidor(porta, ocioso_max):
    """Modelo residente: o Mac manda o áudio pelo agente da Ponte (/whisper) e recebe o JSON."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    trava = threading.Lock()
    ultimo = [time.time()]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, codigo, dados):
            corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            self._json(200, {"ok": True, "modelos": list(MODELOS)})

        def do_POST(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            dados = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                with trava:
                    ultimo[0] = time.time()
                    t = time.time()
                    saida, dur = transcrever(decodificar(dados), q.get("modelo", "large-v3-turbo"), q.get("idioma", "pt"),
                                             int(q["beam"]) if q.get("beam") else None, json.loads(q.get("opcoes", "{}")))
                    saida["_ponte"] = {"segundos": round(time.time() - t, 2), "audio_s": round(dur, 2)}
                    ultimo[0] = time.time()
                self._json(200, saida)
            except Exception as e:
                self._json(500, {"erro": str(e)})

    srv = ThreadingHTTPServer(("127.0.0.1", porta), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    carregar("large-v3-turbo")
    print("servidor whisper pronto na porta %d (desliga após %d min ocioso)" % (porta, ocioso_max // 60), flush=True)
    while time.time() - ultimo[0] < ocioso_max:
        time.sleep(15)
    print("ocioso: desligando e liberando a VRAM", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?")
    ap.add_argument("saida", nargs="?")
    ap.add_argument("--modelo", default="large-v3-turbo")
    ap.add_argument("--idioma", default="pt")
    ap.add_argument("--beam", type=int)
    ap.add_argument("--opcoes", default="{}", help="JSON com opções do transcribe (condition_on_previous_text etc.)")
    ap.add_argument("--servidor", type=int, metavar="PORTA")
    ap.add_argument("--ocioso", type=int, default=1200, help="segundos sem uso até o servidor desligar")
    a = ap.parse_args()
    preparar_cuda()
    if a.servidor:
        return servidor(a.servidor, a.ocioso)
    t0 = time.time()
    carregar(a.modelo)
    t1 = time.time()
    saida, dur = transcrever(decodificar(a.audio), a.modelo, a.idioma, a.beam, json.loads(a.opcoes))
    t2 = time.time()
    Path(a.saida).write_text(json.dumps(saida, ensure_ascii=False), encoding="utf-8")
    print("modelo %.1fs  transcrição %.1fs  áudio %.1fs  segmentos %d" % (t1 - t0, t2 - t1, dur, len(saida["segments"])))


if __name__ == "__main__":
    main()
