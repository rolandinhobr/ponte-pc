"""Substituto do mlx_whisper.transcribe: transcreve no PC (GPU NVIDIA, faster-whisper) quando ele está livre e cai
no MLX do Mac quando o PC está pausado, fora do ar ou falha. Mesma saída: segments com words (word, start, end,
probability).

Medido pelo autor (RTX 5080 contra M3 Max) com 10,6 min de locução: Mac 18,8 s, PC 6,6 s, texto e tempos das
palavras idênticos (mediana 0 ms). Na sua máquina os números serão outros.

Uso nos projetos (troca de uma linha):
    import os, sys; sys.path.insert(0, os.path.expanduser("~/.local/share/ponte"))
    import ponte_whisper as mlx_whisper

PONTE_WHISPER=mac força o Mac; PONTE_WHISPER=pc falha em vez de cair no Mac.
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import pc  # noqa: E402  (o pc.py fica na mesma pasta)

_cache = {"pc_ok": None, "visto": 0.0, "avisou": False}


def _log(msg):
    print("[ponte-whisper] " + msg, file=sys.stderr, flush=True)


def _modelo(repo):
    nome = (repo or "").split("/")[-1].lower()
    if "turbo" in nome or not nome:
        return "large-v3-turbo"
    for chave in ("large-v3", "large-v2", "medium", "small", "base", "tiny"):
        if chave in nome:
            return chave
    return "large-v3-turbo"


def _wav16k(audio):
    """Caminho ou array (16 kHz) -> WAV 16 kHz mono 16-bit, pequeno para a rede."""
    if isinstance(audio, (str, os.PathLike)):
        return subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", "16000",
                               "-c:a", "pcm_s16le", "-f", "wav", "-"], capture_output=True, check=True).stdout
    import numpy as np
    x = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1, 1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _pc_livre(c):
    if time.time() - _cache["visto"] < 20 and _cache["pc_ok"] is not None:
        return _cache["pc_ok"]
    c, e = pc.estado_pc(c)
    g = ((e or {}).get("maquina") or {}).get("gpu") or {}
    # GPU do PC reservada para outro projeto ou sessão: transcreve no Mac sem nem tentar
    ok = bool(e) and not e["pausado"] and (g.get("uso") or 0) < 90 and pc.e_do_dono(e.get("reserva_gpu"))
    _cache.update(pc_ok=ok, visto=time.time())
    return ok


def _servidor_rodando(c):
    _, jobs = pc.chamar(c, "GET", "/jobs?limite=50")
    return any(j["nome"] == "whisper-servidor" and j["estado"] in ("rodando", "na_fila") for j in jobs or [])


def _iniciar_servidor(c):
    if _servidor_rodando(c):
        return
    cmd = ("& (Join-Path $env:PONTE_DIR 'venvs\\whisper\\Scripts\\python.exe') "
           "(Join-Path $env:PONTE_DIR 'whisper_ponte.py') --servidor 17851")
    codigo, j = pc.chamar(c, "POST", "/jobs", {"nome": "whisper-servidor", "comando": cmd, "shell": "pwsh",
                                               "origem": "ponte-whisper"})
    if codigo != 201:
        raise RuntimeError("não subi o servidor whisper: %s" % j)
    _log("subindo o servidor whisper no PC (modelo fica carregado; desliga após 20 min sem uso)")


def _remoto(c, audio, modelo, language, beam, op):
    dados = _wav16k(audio)
    q = {"modelo": modelo, "idioma": language or "pt", "opcoes": json.dumps(op),
         "sessao": pc.sessao_atual(), "dono": os.environ.get("PONTE_DONO", ""),
         "arquivo": audio if isinstance(audio, str) else ""}  # o agente confere a reserva de GPU por estes três
    if beam:
        q["beam"] = str(beam)
    url = pc.base(c) + "/whisper?" + urllib.parse.urlencode(q)
    limite = time.time() + 120
    while True:
        req = urllib.request.Request(url, data=dados, method="POST")
        req.add_header("X-Ponte-Token", c["token"])
        req.add_header("Content-Type", "audio/wav")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                saida = json.loads(r.read())
            saida.pop("_ponte", None)
            return saida
        except urllib.error.HTTPError as e:
            if e.code == 503 and time.time() < limite:  # servidor ainda não está de pé
                _iniciar_servidor(c)
                time.sleep(3)
                continue
            raise RuntimeError("PC respondeu %s: %s" % (e.code, e.read()[:200]))


def _local(audio, **kw):
    import mlx_whisper
    return mlx_whisper.transcribe(audio, **kw)


def transcribe(audio, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", verbose=None, temperature=0.0,
               word_timestamps=False, condition_on_previous_text=True, initial_prompt=None, language=None,
               beam_size=None, **resto):
    kw = dict(path_or_hf_repo=path_or_hf_repo, verbose=verbose, temperature=temperature, word_timestamps=word_timestamps,
              condition_on_previous_text=condition_on_previous_text, initial_prompt=initial_prompt, language=language,
              **resto)
    if beam_size:
        kw["beam_size"] = beam_size
    modo = os.environ.get("PONTE_WHISPER", "auto")
    c = pc.cfg()
    if modo != "mac" and c.get("token"):
        try:
            if modo == "pc" or _pc_livre(c):
                t = time.time()
                temp = temperature if isinstance(temperature, (int, float)) else 0.0
                op = {"word_timestamps": word_timestamps, "condition_on_previous_text": condition_on_previous_text,
                      "temperature": temp, "initial_prompt": initial_prompt}
                # o mlx_whisper usa busca simples por padrão; beam 1 no PC dá o mesmo texto e os mesmos tempos
                saida = _remoto(c, audio, _modelo(path_or_hf_repo), language, beam_size or 1, op)
                if not _cache["avisou"]:
                    _log("transcrevendo no PC: primeiro trecho em %.1fs" % (time.time() - t))
                    _cache["avisou"] = True
                return saida
        except Exception as e:
            if modo == "pc":
                raise
            _cache.update(pc_ok=False, visto=time.time())
            _log("PC indisponível (%s); usando o MLX do Mac" % e)
    return _local(audio, **{k: v for k, v in kw.items() if v is not None or k == "language"})
