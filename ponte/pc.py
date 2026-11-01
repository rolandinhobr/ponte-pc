#!/usr/bin/env python3
"""pc: o despachante da Ponte no computador principal (macOS).

Mede a carga desta máquina e da máquina de trabalho (um PC com Windows), decide onde cada tarefa roda
(pc onde), manda jobs para o agente do Windows, acompanha logs, move arquivos e traduz caminhos do
armazenamento compartilhado (NAS). Tudo que é do seu setup mora em ~/.config/ponte/config.json,
que o install.py cria a partir das suas respostas.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AQUI = Path(__file__).resolve().parent
PASTA_WINDOWS = AQUI / "windows" if (AQUI / "windows").is_dir() else AQUI.parent / "windows"
CONFIG = Path(os.environ.get("PONTE_CONFIG") or Path.home() / ".config" / "ponte" / "config.json")
SSH_CONFIG = Path.home() / ".ssh" / "config"
CHAVE = Path.home() / ".ssh" / "id_ed25519.pub"

# Tudo aqui pode ser trocado em config.json. O install.py preenche a partir das respostas.
PADRAO = {
    "host": "",                 # IP do PC na rede local (o install.py descobre; muda no DHCP, a Ponte reencontra)
    "porta": 17850,
    "token": "",                # segredo compartilhado com o agente; gerado no primeiro `pc instalar`
    "usuario": "",              # usuário do Windows (para o ssh)
    "perfil": "nvidia",         # nvidia | sem-gpu: escolhe a tabela de velocidades (veja perfil_tipos)
    # armazenamento compartilhado (NAS). Deixe nas_host vazio se não tiver: a tradução de caminhos some.
    "nas_host": "",
    "nas_share": "",
    "nas_mac": "",              # onde o share está montado aqui, ex.: /Volumes/MEU-NAS
    "rede": "192.168.0.0/24",   # faixa da rede de casa, onde procurar o PC se o IP mudar
    # fora de casa: o Mac acha o PC pelo Tailscale (precisa estar instalado nas duas máquinas)
    "tailscale": False,
    "host_tailscale": "",       # IP do PC no Tailscale (o install/instalar preenchem)
    "nome_tailscale": "",       # nome do PC no Tailscale (padrão: o hostname do Windows)
    "vazao_fora_padrao": 3.0,   # MB/s de envio fora de casa enquanto a rede ainda não foi medida
    # política: as duas máquinas trabalham juntas; cada tarefa vai para quem tem mais folga x velocidade
    "teto_mac": 70,             # com você usando o Mac, ele não pega trabalho acima disto (% de CPU/GPU)
    "teto_mac_ausente": 85,     # você longe do Mac há 10 min
    "pc_cheio": 95,             # acima disto o PC não recebe mais
    "pc_vram_min_mb": 1200,
    # o que o `pc instalar` põe no PC (o install.py grava conforme as respostas)
    "instalar": {
        "pacotes": ["Python.Python.3.12", "Gyan.FFmpeg", "Git.Git", "Microsoft.PowerShell", "astral-sh.uv"],
        "nas": False,
        "claude_code": False,
        "whisper": False,
        "pausa_jogos": True,
    },
}

# velocidade relativa (PC, Mac) de cada tipo de tarefa. Estes números são do autor (RTX 5080 contra M3 Max, medidos
# com um clipe 1080p 4:2:2 10-bit): servem de ponto de partida, não de verdade. Rode `pc onde` com suas tarefas
# reais, meça e ajuste "velocidade" no config.json: {"encode": [9.5, 7.0]} vale por cima desta tabela.
VELOCIDADE_NVIDIA = {
    "encode": (9.5, 7.0), "4k": (4.4, 2.4), "422": (13.2, 4.8), "av1": (9.7, 0.0), "transcricao": (3.5, 1.0),
    "prores": (11.9, 4.7), "llm-grande": (0.0, 1.0), "cuda": (1.0, 0.0), "ia-imagem": (1.0, 0.25),
    "ia-video": (1.0, 0.15), "llm": (1.0, 0.6), "gui": (1.0, 0.0), "render": (1.0, 1.0), "cpu": (1.0, 1.0),
    "build": (1.0, 1.0), "leve": (1.0, 1.0),
}

# no Mac, estes tipos rodam nos motores de vídeo e na CPU, não na GPU: a GPU cheia (editor, telas) não os impede
MAC_SEM_GPU = {"encode", "4k", "422", "av1", "prores", "cpu", "build", "leve"}

# tipo: (preferência, usa GPU, por quê). Preferência: "mac", "pc", "pc!" (só o PC faz), "carga" (as duas; quem tem folga)
TIPOS = {
    "leve": ("mac", False, "tarefa curta: ir e voltar custa mais que rodar aqui"),
    "prores": ("carga", False, "ProRes: o PC costuma ganhar em CPU; o Mac tem motor de ProRes. Os dois dão conta"),
    "llm-grande": ("mac", False, "modelo maior que a VRAM do PC: a memória unificada do Mac comporta"),
    "cuda": ("pc!", True, "precisa de CUDA"),
    "ia-imagem": ("pc", True, "difusão em CUDA costuma ser muito mais rápida que MPS"),
    "ia-video": ("pc", True, "geração de vídeo em CUDA"),
    "transcricao": ("pc", True, "faster-whisper em CUDA"),
    "llm": ("pc", True, "modelo que cabe na VRAM do PC roda mais rápido lá"),
    "av1": ("pc", True, "encode AV1 por hardware (NVENC) no PC; muitos Macs não têm"),
    "gui": ("pc", False, "operar app com janela no PC deixa a tela do Mac livre"),
    "422": ("pc", True, "H.264/HEVC 4:2:2 10-bit (câmeras): o PC decodifica por hardware (NVDEC); o Mac só em software"),
    "4k": ("pc", True, "upscale ou encode 4K: a GPU do PC leva vantagem"),
    "encode": ("carga", True, "H.264/HEVC 1080p: os dois dão conta"),
    "render": ("carga", True, "render (Blender, Remotion, scripts): os dois dão conta"),
    "cpu": ("carga", False, "lote de CPU"),
    "build": ("carga", False, "build ou testes"),
}


def perfil_tipos(c):
    """(TIPOS, VELOCIDADE) para o perfil escolhido. 'sem-gpu': o PC é só mais CPU (sem NVENC/CUDA), então o que
    exige GPU fica no Mac e o resto se divide 1:1. 'velocidade' no config.json vale por cima de tudo."""
    tipos = dict(TIPOS)
    vel = dict(VELOCIDADE_NVIDIA)
    if c.get("perfil") == "sem-gpu":
        for t, (pref, gpu, porque) in TIPOS.items():
            if pref in ("pc", "pc!") or gpu:
                tipos[t] = ("mac" if pref in ("pc", "pc!") else "carga", False,
                            "PC sem GPU dedicada: " + porque.split(":")[0])
        vel = {t: (1.0, 1.0) for t in TIPOS}
    for t, par in (c.get("velocidade") or {}).items():
        vel[t] = tuple(par)
    return tipos, vel


VAZAO = Path.home() / ".local" / "share" / "ponte" / "vazao.json"


# ---------------------------------------------------------------- funções

def cfg():
    c = dict(PADRAO)
    if CONFIG.exists():
        c.update(json.loads(CONFIG.read_text()))
    return c


def salvar(c):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({k: v for k, v in c.items() if not k.startswith("_")}, indent=2, ensure_ascii=False))
        os.chmod(CONFIG, 0o600)
    except OSError as e:  # sandbox do Codex não grava fora do projeto; segue com a config em memória
        print("aviso: não gravei %s (%s)" % (CONFIG, e.strerror), file=sys.stderr)


def host_ativo(c):
    """Por onde o PC responde agora: o IP da rede de casa ou, fora dela, o do Tailscale (conectar escolhe)."""
    return c.get("_via_host") or c["host"]


def base(c):
    return "http://%s:%s" % (host_ativo(c), c["porta"])


def chamar(c, metodo, rota, dados=None, timeout=8, cru=False):
    if metodo == "POST" and rota == "/jobs" and isinstance(dados, dict):
        # todo job leva a sessão do app que o mandou e o dono (PONTE_DONO), para a reserva de GPU saber de quem é
        dados.setdefault("sessao", sessao_atual())
        dados.setdefault("dono", os.environ.get("PONTE_DONO", ""))
    corpo = json.dumps(dados).encode() if dados is not None else None
    req = urllib.request.Request(base(c) + rota, data=corpo, method=metodo)
    req.add_header("X-Ponte-Token", c["token"])
    if corpo is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            conteudo = r.read()
            return r.status, (conteudo if cru else json.loads(conteudo or b"null"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, {"erro": str(e)}


def prova_token(token, nonce):
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def saude(host, porta, timeout=0.4, token=None):
    """Com token, só aceita o agente que provar que conhece o token (HMAC de um nonce), sem mandá-lo."""
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            pass
        nonce = secrets.token_hex(8)
        url = "http://%s:%s/saude" % (host, porta) + ("?n=" + nonce if token else "")
        with urllib.request.urlopen(url, timeout=2) as r:
            d = json.loads(r.read())
            if token and not hmac.compare_digest(str(d.get("prova") or ""), prova_token(token, nonce)):
                return None
            return d if d.get("ponte") else None
    except Exception:
        return None


def descobrir(c):
    """Varre a rede atrás do agente (o IP do PC pode mudar no DHCP). Qualquer aparelho da rede pode responder
    /saude: só vale quem provar que conhece o token, senão o token iria para ele no próximo pedido."""
    import ipaddress
    rede = ipaddress.ip_network(c["rede"], strict=False)
    with ThreadPoolExecutor(max_workers=200) as ex:
        hosts = [str(h) for h in rede.hosts()]
        for host, achou in zip(hosts, ex.map(lambda h: saude(h, c["porta"], token=c["token"]), hosts)):
            if achou:
                return host
    return None


def gateway():
    m = re.search(r"gateway: (\S+)", _rodar(["route", "-n", "get", "default"]))
    return m.group(1) if m else None


def em_casa(c):
    """O Mac está na rede de casa (o roteador padrão é da faixa da casa)."""
    import ipaddress
    g = gateway()
    try:
        return bool(g) and ipaddress.ip_address(g) in ipaddress.ip_network(c["rede"], strict=False)
    except ValueError:
        return False


def tailscale_bin():
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    return app if os.path.exists(app) else (shutil.which("tailscale") or app)


def tailscale_pc(c):
    """IP do PC no Tailscale: o salvo, ou o que o próprio Tailscale informa pelo nome da máquina."""
    if not c.get("tailscale"):
        return None
    if c.get("host_tailscale") and saude(c["host_tailscale"], c["porta"], timeout=3, token=c["token"]):
        return c["host_tailscale"]
    try:
        st = json.loads(_rodar([tailscale_bin(), "status", "--json"], 5) or "{}")
    except ValueError:
        return None
    for par in (st.get("Peer") or {}).values():
        if (par.get("HostName") or "").lower() == c.get("nome_tailscale", "").lower() and par.get("TailscaleIPs"):
            ip = par["TailscaleIPs"][0]
            if ip != c.get("host_tailscale") and saude(ip, c["porta"], timeout=3, token=c["token"]):
                c["host_tailscale"] = ip
                salvar(c)
                return ip
    return None


def conectar(c, procurar=True):
    """Devolve (config, online). Em casa, pela rede local (se o IP mudou, acha de novo e grava); fora, pelo
    Tailscale. A rota escolhida fica em c["_via"] ("casa" ou "tailscale") e não é gravada."""
    if not c["token"]:
        return c, False
    if c["host"] and saude(c["host"], c["porta"], timeout=1.0):
        c.pop("_via_host", None)
        c["_via"] = "casa"
        return c, True
    ts = tailscale_pc(c)
    if ts:
        c["_via_host"], c["_via"] = ts, "tailscale"
        return c, True
    if procurar and em_casa(c):  # varrer a faixa de casa numa rede estranha só gastaria tempo
        novo = descobrir(c)
        if novo:
            c["host"] = novo
            c["_via"] = "casa"
            salvar(c)
            escrever_ssh(c)
            return c, True
    return c, False


def exigir(c):
    c, online = conectar(c)
    if not online:
        sys.exit("PC fora do ar (desligado, sem rede ou agente parado). Rode aqui no Mac.")
    return c


def sessao_atual():
    """ID da sessão do app Claude que está rodando este comando (local_...), ou vazio fora do app."""
    return os.environ.get("CLAUDE_CODE_HOST_SESSION_ID", "")


def nfc(texto):
    return unicodedata.normalize("NFC", str(texto or "")).casefold().replace("\\", "/")


def e_do_dono(r, dono=None, entradas=()):
    """Esta sessão (ou este trabalho) é de quem reservou a GPU do PC? Mesma regra do agente: sessão, dono ou uma
    das marcas no caminho das entradas."""
    if not r:
        return True
    if sessao_atual() and sessao_atual() in (r.get("sessoes") or []):
        return True
    dono = dono or os.environ.get("PONTE_DONO", "")
    if dono and nfc(dono) == nfc(r.get("dono")):
        return True
    texto = nfc(" ".join(str(x) for x in entradas))
    return any(nfc(m) in texto for m in r.get("marcas") or [] if m)


def origem():
    """'claude:<pasta do projeto>' ou 'codex:<pasta>', para saber depois quem mandou cada job."""
    agente = "codex" if any(k.startswith("CODEX_") for k in os.environ) else "claude" if any(
        k.startswith("CLAUDE") for k in os.environ) else "terminal"
    return "%s:%s" % (agente, unicodedata.normalize("NFC", Path.cwd().name))


def no_nas(nas_mac, caminho):
    """Prefixo com fronteira de pasta: "/Volumes/NAS-1" (segunda montagem do mesmo share) não é o NAS."""
    if not nas_mac:
        return False
    return caminho == nas_mac or caminho.startswith(nas_mac + "/")


def para_windows(c, caminho):
    caminho = unicodedata.normalize("NFC", caminho)  # macOS entrega NFD; NAS costuma guardar NFC
    if no_nas(c["nas_mac"], caminho):
        resto = caminho[len(c["nas_mac"]):].lstrip("/")
        return "\\\\%s\\%s\\%s" % (c["nas_host"], c["nas_share"], resto.replace("/", "\\"))
    return caminho


def para_mac(c, caminho):
    if not c["nas_host"]:
        return caminho
    prefixo = "\\\\%s\\%s" % (c["nas_host"], c["nas_share"])
    if caminho.lower().startswith(prefixo.lower()):
        return c["nas_mac"] + caminho[len(prefixo):].replace("\\", "/")
    return caminho


def traduzir_comando(c, comando):
    """Troca caminhos do NAS entre aspas pela forma UNC do Windows. Dentro das aspas vale a outra aspa (d'Açúcar)
    e a mesma dobrada ('d''Açúcar', como o PowerShell escapa)."""
    if not c["nas_mac"]:
        return comando
    padrao = re.compile(r"([\"'])(%s(?:\1\1|(?!\1).)*)\1" % re.escape(unicodedata.normalize("NFC", c["nas_mac"])))
    comando = unicodedata.normalize("NFC", comando)
    return padrao.sub(lambda m: m.group(1) + para_windows(c, m.group(2)) + m.group(1), comando)


def _rodar(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def estado_mac():
    d = {}
    m = re.findall(r"CPU usage: .*?([\d.]+)% idle", _rodar(["top", "-l", "2", "-n", "0", "-s", "1"], 10))
    media = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
    if m:
        # um pico de 1 s (rsync, Spotlight) não pode decidir sozinho: metade instantâneo, metade média de 1 min
        d["cpu"] = round((100 - float(m[-1]) + media) / 2, 1)
    else:  # sandbox do Codex bloqueia o top; carga média do último minuto por núcleo
        d["cpu"] = round(min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100), 1)
        d["cpu_aprox"] = True
    g = re.findall(r'"Device Utilization %"=(\d+)', _rodar(["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"]))
    d["gpu"] = max(int(x) for x in g) if g else None
    m = re.search(r"free percentage: (\d+)%", _rodar(["memory_pressure", "-Q"]))
    d["mem_livre"] = int(m.group(1)) if m else None
    m = re.search(r'"HIDIdleTime" = (\d+)', _rodar(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    d["ocioso_s"] = int(m.group(1)) // 10**9 if m else None
    interface = re.search(r"interface: (\S+)", _rodar(["route", "-n", "get", "default"]))
    portas = _rodar(["networksetup", "-listallhardwareports"])
    d["rede"] = "?"
    if interface:
        m = re.search(r"Hardware Port: (.+)\nDevice: %s\n" % re.escape(interface.group(1)), portas)
        d["rede"] = "wifi" if m and "Wi-Fi" in m.group(1) else "cabo"
    d["em_casa"] = em_casa(cfg())
    procs = _rodar(["ps", "-Ao", "pcpu=,comm=", "-r"]).strip().splitlines()[:4]
    d["pesados"] = [" ".join(p.split(None, 1)[1:]).split("/")[-1] + " " + p.split()[0] + "%"
                    for p in procs if p.strip() and float(p.split()[0]) > 30]
    return d


def _ticks_cpu():
    """Contadores de CPU do Mac inteiro (usuário, sistema, ocioso, nice), direto do kernel."""
    import ctypes
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.mach_host_self.restype = ctypes.c_uint
    info, n = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
    if lib.host_statistics(lib.mach_host_self(), 3, info, ctypes.byref(n)) != 0:  # 3 = HOST_CPU_LOAD_INFO
        return None
    return list(info)


def estado_mac_leve():
    """Só o que a faixa do processamento mostra (CPU, GPU, memória), sem top nem ps: com 28 sessões abertas o
    top -l 2 de cada uma (1,4 s de CPU por chamada) levou a carga do Mac a 300 (29/10/2026). A CPU sai dos
    contadores do kernel em 1 s, com a mesma conta do estado_mac; o pc onde continua no estado_mac."""
    d = {"leve": True}
    a = _ticks_cpu()
    time.sleep(1)
    b = _ticks_cpu()
    media = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
    delta = [(y - x) % 2**32 for x, y in zip(a, b)] if a and b else []  # contadores de 32 bits dão a volta
    if sum(delta):
        d["cpu"] = round((100 * (1 - delta[2] / sum(delta)) + media) / 2, 1)
    else:
        d["cpu"] = round(media, 1)
        d["cpu_aprox"] = True
    g = re.findall(r'"Device Utilization %"=(\d+)', _rodar(["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"]))
    d["gpu"] = max(int(x) for x in g) if g else None
    m = re.search(r"free percentage: (\d+)%", _rodar(["memory_pressure", "-Q"]))
    d["mem_livre"] = int(m.group(1)) if m else None
    return d


def carga_mac(m, gpu=True):
    """Carga que importa para a tarefa: tarefa só de CPU não foge por causa da GPU cheia."""
    valores = [v for v in (m.get("cpu"), m.get("gpu") if gpu else None) if v is not None]
    carga = max(valores) if valores else 0
    if m.get("mem_livre") is not None and m["mem_livre"] < 15:
        carga = max(carga, 90)
    return carga


def estado_pc(c):
    c, online = conectar(c)
    if not online:
        return c, None
    codigo, e = chamar(c, "GET", "/estado")
    return c, (e if codigo == 200 else None)


def pc_livre(c, e, gpu, nosso=True):
    """(dá pra mandar?, motivo se não). nosso=False: o trabalho não é de quem reservou a GPU do PC."""
    if e is None:
        return False, "PC fora do ar"
    if e["pausado"]:
        return False, "PC pausado (%s)" % e["motivo_pausa"]
    if gpu and not nosso:
        return False, "GPU do PC reservada para %s" % e["reserva_gpu"]["dono"]
    m = e.get("maquina") or {}
    if (m.get("cpu") or 0) >= c["pc_cheio"]:
        return False, "CPU do PC em %s%%" % m["cpu"]
    g = m.get("gpu") or {}
    if gpu and g:
        if (g.get("uso") or 0) >= c["pc_cheio"]:
            return False, "GPU do PC em %s%%" % g["uso"]
        if g.get("vram_total_mb") and g["vram_total_mb"] - (g.get("vram_usada_mb") or 0) < c.get("pc_vram_min_mb", 1200):
            return False, "VRAM do PC quase cheia"
    if e["na_fila"] >= e.get("max_paralelo", 6):
        return False, "PC com fila cheia (%d esperando)" % e["na_fila"]
    return True, ""


def ajustar_tipo(tipo, entradas):
    """Encode/render genérico com origem 4:2:2 ou 4K vira o tipo medido. Devolve (tipo, nota)."""
    if tipo not in ("encode", "render"):
        return tipo, ""
    for arq in entradas:
        if not Path(arq).is_file():
            continue
        saida = _rodar(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,pix_fmt,codec_name", "-of", "json", arq], 20)
        try:
            v = json.loads(saida)["streams"][0]
        except (ValueError, KeyError, IndexError):
            continue
        fmt = v.get("pix_fmt", "")
        if "422" in fmt and v.get("codec_name") in ("h264", "hevc"):
            return "422", "origem %s %s 4:2:2" % (v["codec_name"], fmt)
        if max(v.get("width") or 0, v.get("height") or 0) >= 3800:
            return "4k", "origem %sx%s" % (v.get("width"), v.get("height"))
    return tipo, ""


def vazao_fora(c):
    """MB/s de envio da rede onde o Mac está (fora de casa). Mede uma vez a cada 6 h por rede, em segundo plano
    (networkQuality); enquanto não há medida, usa o padrão conservador."""
    g = gateway() or "?"
    mac_gw = re.search(r"at ([0-9a-f:]{11,17})", _rodar(["arp", "-n", g])) if g != "?" else None
    chave = "%s|%s" % (g, mac_gw.group(1) if mac_gw else "")
    try:
        medidas = json.loads(VAZAO.read_text())
    except (OSError, ValueError):
        medidas = {}
    d = medidas.get(chave)
    if d and time.time() - d["quando"] < 6 * 3600:
        return max(0.5, d["mbs"])
    if not d or time.time() - d.get("tentativa", 0) > 600:
        medidas[chave] = dict(d or {}, tentativa=time.time(), mbs=(d or {}).get("mbs", c["vazao_fora_padrao"]),
                              quando=(d or {}).get("quando", 0))
        VAZAO.parent.mkdir(parents=True, exist_ok=True)
        VAZAO.write_text(json.dumps(medidas))
        script = ("import json,subprocess,time,sys;from pathlib import Path\n"
                  "r=json.loads(subprocess.run(['networkQuality','-c','-s'],capture_output=True,text=True,timeout=120).stdout)\n"
                  "p=Path(sys.argv[1]);m=json.loads(p.read_text())\n"
                  "m[sys.argv[2]]={'mbs':r['ul_throughput']/8/2**20*0.8,'quando':time.time(),'tentativa':time.time()}\n"
                  "p.write_text(json.dumps(m))\n")
        subprocess.Popen(["nice", "-n", "15", sys.executable, "-c", script, str(VAZAO), chave],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return (d or {}).get("mbs") or c["vazao_fora_padrao"]


def decidir(c, tipo, minutos=None, entradas=(), lote=None, dono=None):
    """As duas máquinas trabalham ao mesmo tempo. Cada tarefa vai para quem tem mais folga, pesada pela
    velocidade; o Mac só pega trabalho até o teto que preserva o seu uso."""
    tipo, nota = ajustar_tipo(tipo, entradas)
    tipos, velocidade = perfil_tipos(c)
    pref, gpu, porque = tipos[tipo]
    if nota:
        porque = "%s (%s)" % (porque, nota)
    m = estado_mac()
    c, e = estado_pc(c)
    carga = carga_mac(m, gpu and tipo not in MAC_SEM_GPU)
    ausente = (m.get("ocioso_s") or 0) >= 600
    teto = c["teto_mac_ausente"] if ausente else c["teto_mac"]
    mac_ok = carga < teto
    # GPU do PC reservada para outro projeto ou sessão: trabalho de GPU dos outros não entra nem na fila do PC
    nosso = e_do_dono((e or {}).get("reserva_gpu"), dono, entradas)
    livre, motivo_pc = pc_livre(c, e, gpu, nosso)
    pc_aceita = bool(e) and not e["pausado"] and (nosso or not gpu)  # pode receber, mesmo que entre na fila
    vel_pc, vel_mac = velocidade.get(tipo, (1.0, 1.0))

    pc_carga = 100
    if e:
        mq = e.get("maquina") or {}
        medida = max(mq.get("cpu") or 0, ((mq.get("gpu") or {}).get("uso") or 0) if gpu else 0)
        pc_carga = max(medida, 35 * e["rodando"])  # job recém-lançado ainda não aparece na medida
    folga_pc = max(0, c["pc_cheio"] - pc_carga) if livre else 0
    folga_mac = max(0, teto - carga)

    sem_nas = bool(c["nas_mac"]) and not os.path.ismount(c["nas_mac"]) and any(no_nas(c["nas_mac"], str(a)) for a in entradas)
    if sem_nas:  # fora de casa o Mac não vê o NAS; o PC está do lado dele
        mac_ok, folga_mac, vel_mac = False, 0, 0
    bytes_locais = 0
    for arq in entradas:
        p = Path(arq)
        if p.exists() and not no_nas(c["nas_mac"], str(p.resolve())):
            bytes_locais += p.stat().st_size if p.is_file() else sum(x.stat().st_size for x in p.rglob("*") if x.is_file())
    if m.get("em_casa", True):
        mbps = 100 if m["rede"] == "cabo" else 25
    else:  # fora de casa o envio sai pela internet de onde o Mac estiver (medida e guardada por rede)
        mbps = vazao_fora(c) if bytes_locais else c["vazao_fora_padrao"]
    envio_s = bytes_locais / (mbps * 2**20)
    envio_caro = minutos is not None and envio_s > minutos * 60 * 0.5

    folga_pc = max(0, c["pc_cheio"] - pc_carga) if livre else 0  # recalculada com o teto do PC no talo

    def r(onde, motivo, modo="normal"):
        d = {"onde": onde, "modo": modo, "motivo": motivo, "tipo": tipo, "carga_mac": carga, "teto_mac": teto,
             "folga_mac": folga_mac, "folga_pc": folga_pc, "envio_s": round(envio_s), "mac": m, "pc": e}
        if lote:
            d["lote"] = dividir(lote)
        return d

    def dividir(n):
        """As duas juntas: o lote se divide na proporção velocidade x folga de cada máquina."""
        if pref == "mac" and (mac_ok or not livre):
            return {"pc": 0, "mac": n}  # igual à rota única: tarefa leve fica no Mac enquanto ele tem folga
        s_pc = vel_pc * (max(folga_pc, 30) if pc_aceita else 0)
        s_mac = vel_mac * folga_mac
        if tipo == "llm-grande":
            s_pc = 0
        if s_pc + s_mac == 0:
            if pc_aceita and vel_pc:
                return {"pc": n, "mac": 0}
            return {"pc": 0, "mac": n if vel_mac else 0}  # o que o Mac não faz espera o PC voltar
        n_pc = int(round(n * s_pc / (s_pc + s_mac)))
        return {"pc": n_pc, "mac": n - n_pc}

    estado_mac_txt = "Mac em %d%% (teto %d%%)" % (carga, teto)
    nice = "nice" if carga >= teto - 15 else "normal"
    if sem_nas:
        if pc_aceita:
            return r("pc", "material no NAS e o Mac fora de casa, sem o NAS: roda no PC, ao lado do NAS" +
                     ("" if livre else "; entra na fila do PC (%s)" % motivo_pc))
        return r("esperar", "material no NAS, o Mac sem o NAS e o PC indisponível (%s)" % motivo_pc)

    if pref == "mac":
        if mac_ok or tipo == "llm-grande" or not livre:
            return r("mac", "%s; %s" % (porque, estado_mac_txt), nice)
        return r("pc", "%s, mas o Mac passou do teto (%s) e o PC está livre" % (porque, estado_mac_txt))
    if pref == "pc!":
        if pc_aceita:
            return r("pc", porque + ("" if livre else "; entra na fila do PC (%s)" % motivo_pc))
        return r("mac" if mac_ok else "esperar", "%s e %s: versão MPS/MLX aqui ou esperar" % (porque, motivo_pc))
    if pref == "pc":
        # sem versão no Mac (AV1, GUI), esperar não barateia o envio: vai ao PC mesmo assim
        if livre and (not envio_caro or not vel_mac):
            return r("pc", porque)
        rede_txt = m["rede"] if m.get("em_casa", True) else "internet de fora de casa, %.1f MB/s" % mbps
        motivo = "envio de %ds pela %s não compensa" % (envio_s, rede_txt) if livre else motivo_pc
        if mac_ok and vel_mac:
            return r("mac", "%s; o Mac tem folga e trabalha junto (%s)" % (motivo, estado_mac_txt), nice)
        if pc_aceita and (not envio_caro or not vel_mac):
            return r("pc", "%s; Mac sem folga (%s): entra na fila do PC" % (motivo, estado_mac_txt))
        return r("esperar", "%s e %s" % (motivo, estado_mac_txt))
    # "carga": as duas fazem; vai para quem tem mais folga x velocidade
    if minutos is not None and minutos < 1 and mac_ok:
        return r("mac", "tarefa curta; %s" % estado_mac_txt, nice)
    if livre and not envio_caro and mac_ok and vel_pc and vel_mac:
        if vel_pc * folga_pc >= vel_mac * folga_mac:
            return r("pc", "PC com mais folga x velocidade (%d%% x %.1f) que o %s; o Mac fica livre para o próximo" %
                     (folga_pc, vel_pc, estado_mac_txt))
        return r("mac", "Mac com mais folga x velocidade (%s) que o PC (%d%% livre)" % (estado_mac_txt, folga_pc), nice)
    if livre and not envio_caro and vel_pc:
        return r("pc", "Mac sem folga (%s); PC livre" % estado_mac_txt)
    motivo = "envio de %ds não compensa" % envio_s if livre else motivo_pc
    if mac_ok:
        return r("mac", "%s; o Mac trabalha junto (%s)" % (motivo, estado_mac_txt), nice)
    if pc_aceita and not envio_caro:
        return r("pc", "Mac sem folga (%s): entra na fila do PC" % estado_mac_txt)
    return r("esperar", "%s e %s: esperar quem liberar primeiro" % (motivo, estado_mac_txt))


def fmt_pc(e):
    if e is None:
        return "PC: fora do ar"
    m = e.get("maquina") or {}
    g = m.get("gpu") or {}
    partes = ["PC %s" % e["host"]]
    partes.append("PAUSADO (%s)" % e["motivo_pausa"] if e["pausado"] else "ativo")
    if m.get("cpu") is not None:
        partes.append("CPU %s%%" % m["cpu"])
    if g:
        partes.append("GPU %s%%  VRAM %.1f/%.0f GB  %s°C" % (g.get("uso"), (g.get("vram_usada_mb") or 0) / 1024,
                                                          (g.get("vram_total_mb") or 0) / 1024, g.get("temp")))
    if m.get("ram") is not None:
        partes.append("RAM %s%%" % m["ram"])
    partes.append("jobs %d rodando, %d na fila" % (e["rodando"], e["na_fila"]))
    if e.get("nas_ok") is not None:
        partes.append("NAS ok" if e["nas_ok"] else "sem NAS")
    txt = "  ".join(partes)
    r = e.get("reserva_gpu")
    if r:
        txt += "\n     " + fmt_reserva(r)
    return txt


def fmt_reserva(r):
    ate = time.strftime("até %d/%m %H:%M", time.localtime(r["ate"])) if r.get("ate") else "sem prazo"
    txt = "GPU RESERVADA para %s (%s)" % (r["dono"], ate)
    if e_do_dono(r):
        txt += ", esta sessão está na reserva"
    detalhes = []
    if r.get("do_dono_rodando"):
        detalhes.append("%d job(s) do dono rodando" % r["do_dono_rodando"])
    if r.get("outros_na_gpu"):
        detalhes.append("%d job(s) de GPU de outros terminando em prioridade baixa" % r["outros_na_gpu"])
    if r.get("retidos"):
        detalhes.append("%d job(s) de GPU de outros esperando a reserva acabar" % r["retidos"])
    return txt + ("; " + "; ".join(detalhes) if detalhes else "")


def fmt_mac(m):
    txt = "Mac  CPU %s%%  GPU %s%%  memória livre %s%%  rede %s%s" % (m["cpu"], m["gpu"], m["mem_livre"], m.get("rede", "?"),
                                                                  "" if m.get("em_casa", True) else " (fora de casa)")
    if m.get("ocioso_s") is not None and m["ocioso_s"] >= 600:
        txt += "  (você longe há %d min)" % (m["ocioso_s"] // 60)
    if m.get("pesados"):
        txt += "\n     pesados: " + ", ".join(m["pesados"])
    return txt


def cmd_estado(a):
    c = cfg()
    m = estado_mac_leve() if a.leve else estado_mac()
    c, e = estado_pc(c)
    if a.json:
        print(json.dumps({"mac": m, "pc": e, "via": c.get("_via")}, ensure_ascii=False, indent=2))
        return
    print(fmt_mac(m))
    print(fmt_pc(e) + ("  [pelo Tailscale: Mac fora de casa]" if e and c.get("_via") == "tailscale" else ""))


def cmd_reserva_gpu(a):
    """Reserva a GPU do PC para um projeto ou sessões: os jobs dele passam na frente, ganham prioridade alta e são
    os únicos que entram na GPU; trabalho de GPU dos outros vai para o Mac (pc onde) ou espera na fila."""
    c = exigir(cfg())
    if a.soltar:
        codigo, d = chamar(c, "POST", "/reserva", {"soltar": True})
        if codigo != 200:
            sys.exit("o PC recusou: %s" % d)
        print("GPU do PC solta: volta à fila normal, por ordem de chegada")
        return
    if not a.dono:
        _, e = estado_pc(c)
        r = (e or {}).get("reserva_gpu")
        print(fmt_reserva(r) if r else "GPU do PC sem reserva (fila normal, por ordem de chegada)")
        return
    sessoes = list(a.sessao or [])
    if not a.sem_esta_sessao and sessao_atual() and sessao_atual() not in sessoes:
        sessoes.insert(0, sessao_atual())
    marcas = a.marca if a.marca is not None else [a.dono]
    codigo, d = chamar(c, "POST", "/reserva", {"dono": a.dono, "marcas": marcas, "sessoes": sessoes,
                                               "horas": a.horas})
    if codigo != 200:
        sys.exit("o PC recusou: %s" % d)
    print(fmt_reserva(d["reserva_gpu"]))
    print("  sessões: %s" % (", ".join(sessoes) or "nenhuma"))
    print("  marcas: %s" % (", ".join(marcas) or "nenhuma"))


def cmd_onde(a):
    r = decidir(cfg(), a.tipo, a.min, a.entrada or (), a.lote, a.dono)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    modo = " (com nice -n 10)" if r["modo"] == "nice" and r["onde"] == "mac" else ""
    if a.lote:
        d = r["lote"]
        if not d["pc"] + d["mac"]:  # nenhuma das duas faz agora (ex.: AV1 com o PC fora)
            print("ESPERAR: %s" % r["motivo"])
            return
        print("LOTE de %d: %d no PC e %d no Mac (velocidade x folga; Mac em %d%%, teto %d%%)%s" %
              (a.lote, d["pc"], d["mac"], r["carga_mac"], r["teto_mac"], modo))
        return
    print("%s%s: %s" % (r["onde"].upper(), modo, r["motivo"]))


def acompanhar(c, jid, quieto=False):
    pos = 0
    sem_resposta, aviso = None, 0
    while True:
        try:
            codigo, d = chamar(c, "GET", "/jobs/%s/log?desde=%d" % (jid, pos), timeout=15)
        except Exception as e:
            sem_resposta = sem_resposta or time.time()
            if time.time() - aviso >= 60:  # a cada minuto sem resposta: avisa e procura o PC (o IP pode ter mudado)
                aviso = time.time()
                print("[ponte] PC sem resposta há %ds (%s); tentando de novo" % (aviso - sem_resposta, e), file=sys.stderr)
                c = conectar(c)[0]
            time.sleep(3)
            continue
        sem_resposta, aviso = None, 0
        if codigo != 200:
            sys.exit("job %s: %s" % (jid, d))
        if d["texto"] and not quieto:
            sys.stdout.write(d["texto"])
            sys.stdout.flush()
        pos = d["proximo"]
        if d["estado"] not in ("rodando", "na_fila") and not d["texto"]:
            if not quieto:
                print("\n[%s] %s, código %s" % (jid, d["estado"], d["codigo"]), file=sys.stderr)
            return d["codigo"] if d["codigo"] is not None else 1
        time.sleep(0.5 if d["texto"] else 2)


def cmd_roda(a):
    c = exigir(cfg())
    comando = juntar_comando(a.comando, a.shell).strip()
    if not comando:
        sys.exit("faltou o comando, ex.: pc roda -- ffmpeg -i entrada.mov saida.mp4")
    env = dict(kv.split("=", 1) for kv in a.env or [])
    pedido = {"nome": a.nome, "comando": traduzir_comando(c, comando), "shell": a.shell, "gpu": a.gpu, "env": env,
              "cwd": para_windows(c, a.cwd) if a.cwd else None, "mesmo_pausado": a.mesmo_pausado,
              "origem": origem(), "dono": a.dono or os.environ.get("PONTE_DONO", "")}
    codigo, j = chamar(c, "POST", "/jobs", pedido)
    if codigo == 409:
        sys.exit("PC pausado (%s). Rode aqui no Mac ou use --mesmo-pausado." % j.get("motivo"))
    if codigo != 201:
        sys.exit("o PC recusou: %s" % j)
    if a.gpu:
        _, e = estado_pc(c)
        r = (e or {}).get("reserva_gpu")
        if r and not e_do_dono(r, a.dono, [comando, a.cwd or "", a.nome or ""]):
            print("[ponte] GPU do PC reservada para %s: este job espera na fila até a reserva acabar "
                  "(cancele com pc cancela %s e rode no Mac)" % (r["dono"], j["id"]), file=sys.stderr)
    print(j["id"])
    if a.espera:
        sys.exit(acompanhar(c, j["id"]))


def q_ps(x):
    return "'" + x.replace("'", "''") + "'"


def juntar_comando(partes, shell="pwsh"):
    """Um argumento só é o comando inteiro, como veio. Vários são o argv que o shell do Mac já tirou das aspas: os
    que têm espaço ou aspas voltam entre aspas no shell do PC (senão "/Volumes/MEU NAS/x" chega partido)."""
    if len(partes) <= 1:
        return "".join(partes)
    import shlex

    def aspas(x):
        if x and not re.search(r"[\s\"']", x):
            return x
        return '"%s"' % x if shell == "cmd" else shlex.quote(x) if shell == "wsl" else q_ps(x)
    saida = [aspas(x) for x in partes]
    if shell == "pwsh" and saida[0] != partes[0]:
        saida[0] = "& " + saida[0]  # no PowerShell, texto entre aspas no começo é valor, não comando
    return " ".join(saida)


def cmd_jobs(a):
    c, online = conectar(cfg())
    jobs = chamar(c, "GET", "/jobs?limite=%d" % a.n)[1] if online else []
    for j in jobs:
        dur = ""
        if j.get("inicio"):
            dur = "%ds" % ((j.get("fim") or time.time()) - j["inicio"])
        print("%-18s %-10s %6s  %-14s %-28s %s" % (j["id"], j["estado"], dur, j["nome"][:14], (j.get("origem") or "")[:28],
                                                  j["comando"].replace("\n", " ")[:50]))


def cmd_log(a):
    c = exigir(cfg())
    if a.f:
        sys.exit(acompanhar(c, a.id))
    _, d = chamar(c, "GET", "/jobs/%s/log" % a.id, timeout=30)
    sys.stdout.write(d.get("texto", ""))
    print("\n[%s] %s, código %s" % (a.id, d.get("estado"), d.get("codigo")), file=sys.stderr)


def cmd_espera(a):
    c = exigir(cfg())
    sys.exit(acompanhar(c, a.id, quieto=a.quieto))


def cmd_cancela(a):
    c = exigir(cfg())
    print(chamar(c, "POST", "/jobs/%s/cancelar" % a.id)[1].get("estado"))


def _curl(c, args):
    # o token vai pelo stdin (-K -), não pela linha de comando, que qualquer processo do Mac lê no ps
    base_args = ["curl", "--fail-with-body", "-sS", "--connect-timeout", "5", "--speed-time", "60", "--speed-limit", "1024",
                 "-K", "-"]
    if sys.stderr.isatty():
        base_args[3:3] = ["--progress-bar"]
        base_args.remove("-sS")
    return subprocess.run(base_args + args, input='header = "X-Ponte-Token: %s"\n' % c["token"], text=True).returncode


def cmd_envia(a):
    c = exigir(cfg())
    origem = Path(a.local)
    destino = para_windows(c, a.remoto) if a.remoto else origem.name
    url = base(c) + "/arquivos?caminho=" + urllib.request.quote(destino)
    sys.exit(_curl(c, ["-T", str(origem), url, "-o", "/dev/stdout"]))


def cmd_traz(a):
    c = exigir(cfg())
    remoto = para_windows(c, a.remoto)
    local = a.local or remoto.replace("\\", "/").split("/")[-1]
    url = base(c) + "/arquivos?caminho=" + urllib.request.quote(remoto)
    sys.exit(_curl(c, [url, "-o", local]))


def cmd_pausa(a, valor=True):
    c = exigir(cfg())
    print(fmt_pc(chamar(c, "POST", "/pausa", {"pausado": valor})[1]))


def cmd_tela(a):
    c = exigir(cfg())
    codigo, png = chamar(c, "GET", "/tela", timeout=20, cru=True)
    if codigo != 200:
        sys.exit("sem tela: %s" % png)
    Path(a.arquivo).write_bytes(png)
    print(a.arquivo)


def cmd_caminho(a):
    c = cfg()
    print(para_mac(c, a.caminho) if a.caminho.startswith("\\\\") else para_windows(c, a.caminho))


def cmd_atualiza(a):
    c = exigir(cfg())
    _, jobs = chamar(c, "GET", "/jobs?limite=100")
    ativos = [j for j in jobs or [] if j["estado"] in ("rodando", "na_fila") and j["nome"] != "whisper-servidor"]
    if ativos and not a.forcar:
        for j in ativos:
            print("  %s  %-16s %s" % (j["id"], j["nome"][:16], j.get("origem", "")))
        sys.exit("há %d job(s) rodando no PC; reiniciar o agente mataria esses jobs. Espere terminar ou use --forcar." % len(ativos))
    arquivos = [str(p) for p in (PASTA_WINDOWS / n for n in ("agente.py", "whisper_ponte.py")) if p.exists()]
    r = subprocess.call(["scp", "-q"] + arquivos + ["pc:AppData/Local/Ponte/"])
    if r:
        sys.exit("scp falhou")
    subprocess.call(["ssh", "pc", "Stop-ScheduledTask -TaskName 'Ponte Agente'; Start-Sleep 2; "
                                  "Start-ScheduledTask -TaskName 'Ponte Agente'"])
    for _ in range(15):
        time.sleep(1)
        d = saude(host_ativo(c), c["porta"], 1.0)
        if d:
            print("agente %s no ar" % d["ponte"])
            return
    sys.exit("o agente não voltou; olhe AppData\\Local\\Ponte\\agente.log no PC")


def escrever_ssh(c):
    if not c.get("usuario") or not c.get("host"):
        return
    # em casa vai direto pelo IP local; fora (o IP local não responde em 1 s), pelo Tailscale
    lan = ("Match host pc exec \"nc -z -G 1 %s 22 >/dev/null 2>&1\"\n  HostName %s\n" % (c["host"], c["host"])
           if c.get("host_tailscale") else "")
    bloco = ("# >>> ponte\n%sHost pc\n  HostName %s\n  User %s\n  IdentityFile ~/.ssh/id_ed25519\n"
             "  StrictHostKeyChecking accept-new\n  ServerAliveInterval 30\n  ConnectTimeout 5\n# <<< ponte\n"
             % (lan, c.get("host_tailscale") or c["host"], c["usuario"]))
    try:
        SSH_CONFIG.parent.mkdir(mode=0o700, exist_ok=True)
        atual = SSH_CONFIG.read_text() if SSH_CONFIG.exists() else ""
        atual = re.sub(r"# >>> ponte\n.*?# <<< ponte\n", "", atual, flags=re.S)
        SSH_CONFIG.write_text(bloco + atual)
        os.chmod(SSH_CONFIG, 0o600)
    except OSError as e:
        print("aviso: não atualizei %s (%s)" % (SSH_CONFIG, e.strerror), file=sys.stderr)


def ip_do_mac():
    m = re.search(r"interface: (\S+)", _rodar(["route", "-n", "get", "default"]))
    return _rodar(["ipconfig", "getifaddr", m.group(1)]).strip() if m else ""


def cmd_instalar(a):
    c = cfg()
    if not c["token"]:
        c["token"] = secrets.token_urlsafe(24)
        salvar(c)
    if not CHAVE.exists():
        sys.exit("não achei %s" % CHAVE)
    ip = ip_do_mac()
    origem = "http://%s:%d" % (ip, a.porta)
    modelo = (PASTA_WINDOWS / "instalar.ps1").read_text(encoding="utf-8")
    inst = c.get("instalar") or {}
    valores = {"__TOKEN__": c["token"], "__CHAVE__": CHAVE.read_text().strip(),
               "__NASHOST__": c["nas_host"], "__NASSHARE__": c["nas_share"], "__PORTA__": str(c["porta"]),
               "__PACOTES__": ",".join("'%s'" % p for p in inst.get("pacotes", [])) or "@()",
               "__USARNAS__": "$true" if inst.get("nas") and c["nas_host"] else "$false",
               "__CLAUDECODE__": "$true" if inst.get("claude_code") else "$false",
               "__WHISPER__": "$true" if inst.get("whisper") else "$false",
               "__TAILSCALE__": "$true" if c.get("tailscale") else "$false",
               "__PAUSAJOGOS__": "$true" if inst.get("pausa_jogos", True) else "$false"}
    codigo_ps = {"__PACOTES__", "__USARNAS__", "__CLAUDECODE__", "__WHISPER__", "__TAILSCALE__", "__PAUSAJOGOS__",
                 "__PORTA__"}
    for k, v in valores.items():  # textos entram entre aspas simples do PowerShell; os marcados acima são código
        modelo = modelo.replace(k, v if k in codigo_ps else v.replace("'", "''"))
    def script(host):
        # o endereço vem de como o PC chegou aqui: o IP do Mac muda entre cabo e Wi-Fi
        # sem BOM: o irm já decodifica UTF-8 pelo charset, e o BOM viraria um comando "\ufeff#"
        return modelo.replace("__BASE__", "http://" + host).encode("utf-8")
    pronto = threading.Event()
    info = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, formato, *args):
            print("  [instalador] " + formato % args, file=sys.stderr)

        def _enviar(self, corpo, tipo):
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            if self.path == "/instalar.ps1":
                host = self.headers.get("Host") or "%s:%d" % (ip, a.porta)
                return self._enviar(script(host), "text/plain; charset=utf-8")
            if self.path in ("/agente.py", "/whisper_ponte.py") and (PASTA_WINDOWS / self.path[1:]).exists():
                return self._enviar((PASTA_WINDOWS / self.path[1:]).read_bytes(), "text/plain; charset=utf-8")
            self.send_error(404)

        def do_POST(self):
            if self.path != "/pronto":
                return self.send_error(404)
            n = int(self.headers.get("Content-Length") or 0)
            info["mac_visto"] = self.headers.get("Host")
            info.update(json.loads(self.rfile.read(n).decode("utf-8-sig")))
            self._enviar(b'"ok"', "application/json")
            pronto.set()

    servidor = ThreadingHTTPServer(("0.0.0.0", a.porta), H)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    print("Enquanto esta janela espera, o instalador (com o token e a sua chave pública) fica servido na rede local: "
          "faça o passo abaixo agora e feche com Ctrl+C se desistir.\n")
    print("No PC: botão direito no Iniciar > Terminal (Admin), cole e dê Enter:\n")
    print("    irm %s/instalar.ps1 | iex\n" % origem, flush=True)
    if not pronto.wait(a.espera * 60):
        servidor.shutdown()
        sys.exit("o PC não avisou em %d min" % a.espera)
    time.sleep(1)
    servidor.shutdown()
    c.update(host=info["ip"], usuario=info["usuario"], hostname=info.get("hostname"), gpu=info.get("gpu"),
             cpu=info.get("cpu"), ram_gb=info.get("ram_gb"))
    if c.get("tailscale"):
        if info.get("tailscale_ip") and not c.get("host_tailscale"):
            c["host_tailscale"] = info["tailscale_ip"]
        if info.get("hostname") and not c.get("nome_tailscale"):
            c["nome_tailscale"] = info["hostname"].lower()
    salvar(c)
    escrever_ssh(c)
    print("PC avisou:", json.dumps(info, ensure_ascii=False))
    ssh = subprocess.run(["ssh", "-o", "BatchMode=yes", "pc", "echo ssh-ok"], capture_output=True, text=True, timeout=30)
    print("ssh:", (ssh.stdout or ssh.stderr).strip())
    c, e = estado_pc(c)
    print(fmt_pc(e))


# ---------------------------------------------------------------- argumentos

def cmd_config(a):
    """Mostra a configuração em uso (sem o token) e de onde ela veio."""
    c = {k: v for k, v in cfg().items() if k != "token"}
    c["token"] = "(definido)" if cfg().get("token") else "(vazio: rode pc instalar)"
    print("# %s" % CONFIG)
    print(json.dumps(c, indent=2, ensure_ascii=False))


def cmd_doctor(a):
    """Confere o que a Ponte precisa, do lado do Mac e do PC, e diz o que falta."""
    c = cfg()
    ok = True

    def linha(bom, texto, dica=""):
        nonlocal ok
        ok = ok and bom
        print("%s %s%s" % ("ok   " if bom else "FALTA", texto, ("  -> " + dica) if dica and not bom else ""))
    linha(CONFIG.exists(), "config em %s" % CONFIG, "rode install.py")
    linha(bool(c["token"]), "token definido", "rode pc instalar")
    linha(CHAVE.exists(), "chave ssh %s" % CHAVE, "ssh-keygen -t ed25519")
    for exe in ("ffmpeg", "ffprobe", "ssh", "scp", "curl"):
        linha(bool(shutil.which(exe)), "%s no PATH" % exe, "brew install ffmpeg" if "ff" in exe else "")
    if c["nas_mac"]:
        linha(os.path.ismount(c["nas_mac"]) or os.path.isdir(c["nas_mac"]), "NAS montado em %s" % c["nas_mac"],
              "monte o share no Finder (Cmd+K)")
    c, online = conectar(c)
    linha(online, "PC responde%s" % (" (%s, via %s)" % (host_ativo(c), c.get("_via")) if online else ""),
          "ligue o PC e confira o agente (ícone na bandeja)")
    if online:
        _, e = estado_pc(c)
        if e:
            print("     " + fmt_pc(e).replace("\n", "\n     "))
            linha(e.get("nas_ok") is not False, "PC enxerga o NAS", "cmdkey /add:<nas> no PC")
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(prog="pc", description="Ponte: decide onde rodar e delega para o PC.")
    s = p.add_subparsers(dest="cmd", required=True)
    tipos, _ = perfil_tipos(cfg())

    x = s.add_parser("estado", help="carga do Mac e do PC agora")
    x.add_argument("--json", action="store_true")
    x.add_argument("--leve", action="store_true", help="só CPU, GPU e memória do Mac, sem top nem ps")
    x.set_defaults(f=cmd_estado)

    x = s.add_parser("onde", help="decide onde uma tarefa deve rodar")
    x.add_argument("tipo", choices=sorted(tipos))
    x.add_argument("--min", type=float, help="duração estimada em minutos")
    x.add_argument("--entrada", action="append", help="arquivo de entrada (mede o custo do envio e detecta 4:2:2/4K)")
    x.add_argument("--lote", type=int, metavar="N", help="divide N itens entre PC e Mac")
    x.add_argument("--dono", help="projeto do trabalho (vale com a GPU do PC reservada; padrão: $PONTE_DONO)")
    x.add_argument("--json", action="store_true")
    x.set_defaults(f=cmd_onde)

    x = s.add_parser("reserva-gpu", help="reserva a GPU do PC para um projeto (sem argumentos: mostra)")
    x.add_argument("--dono", help="nome do projeto, ex.: \"MEU FILME\"")
    x.add_argument("--marca", action="append",
                   help="trecho que identifica os jobs do dono no comando ou na pasta (padrão: o próprio --dono)")
    x.add_argument("--sessao", action="append", help="outra sessão do app (local_...) que entra na reserva")
    x.add_argument("--sem-esta-sessao", action="store_true", help="não inclui a sessão que está rodando o comando")
    x.add_argument("--horas", type=float, default=72, help="validade em horas (padrão 72; 0 = até soltar)")
    x.add_argument("--soltar", action="store_true", help="acaba com a reserva")
    x.set_defaults(f=cmd_reserva_gpu)

    x = s.add_parser("roda", help="manda um comando para o PC")
    x.add_argument("--nome")
    x.add_argument("--shell", choices=["pwsh", "cmd", "wsl"], default="pwsh")
    x.add_argument("--cwd", help="pasta de trabalho no PC (aceita caminho do NAS no Mac)")
    x.add_argument("--gpu", action="store_true", help="ocupa uma vaga de GPU no PC")
    x.add_argument("--dono", help="projeto do trabalho, para a reserva de GPU (padrão: $PONTE_DONO)")
    x.add_argument("--env", action="append", help="VAR=valor")
    x.add_argument("--espera", action="store_true", help="acompanha o log e devolve o código do job")
    x.add_argument("--mesmo-pausado", action="store_true")
    x.add_argument("comando", nargs=argparse.REMAINDER)
    x.set_defaults(f=cmd_roda)

    x = s.add_parser("jobs", help="últimos jobs no PC")
    x.add_argument("-n", type=int, default=15)
    x.set_defaults(f=cmd_jobs)

    x = s.add_parser("log", help="log de um job")
    x.add_argument("id")
    x.add_argument("-f", action="store_true", help="acompanhar")
    x.set_defaults(f=cmd_log)

    x = s.add_parser("espera", help="espera um job terminar")
    x.add_argument("id")
    x.add_argument("--quieto", action="store_true")
    x.set_defaults(f=cmd_espera)

    x = s.add_parser("cancela", help="cancela um job")
    x.add_argument("id")
    x.set_defaults(f=cmd_cancela)

    x = s.add_parser("envia", help="manda arquivo do Mac direto para o PC")
    x.add_argument("local")
    x.add_argument("remoto", nargs="?", help="caminho no PC (padrão: pasta area da Ponte)")
    x.set_defaults(f=cmd_envia)

    x = s.add_parser("traz", help="traz arquivo do PC para o Mac")
    x.add_argument("remoto")
    x.add_argument("local", nargs="?")
    x.set_defaults(f=cmd_traz)

    s.add_parser("pausa", help="tira o PC do processo").set_defaults(f=lambda a: cmd_pausa(a, True))
    s.add_parser("retoma", help="devolve o PC ao processo").set_defaults(f=lambda a: cmd_pausa(a, False))

    x = s.add_parser("tela", help="captura a tela do PC")
    x.add_argument("arquivo", nargs="?", default="/tmp/pc-tela.png")
    x.set_defaults(f=cmd_tela)

    x = s.add_parser("caminho", help="traduz caminho do NAS entre Mac e Windows")
    x.add_argument("caminho")
    x.set_defaults(f=cmd_caminho)

    s.add_parser("config", help="mostra a configuração em uso").set_defaults(f=cmd_config)
    s.add_parser("doctor", help="confere o que falta no Mac e no PC").set_defaults(f=cmd_doctor)

    x = s.add_parser("atualiza", help="manda a versão nova do agente e reinicia (recusa se houver job rodando)")
    x.add_argument("--forcar", action="store_true", help="reinicia mesmo matando os jobs em andamento")
    x.set_defaults(f=cmd_atualiza)

    x = s.add_parser("instalar", help="serve o instalador para o PC e espera ele avisar")
    x.add_argument("--porta", type=int, default=8765)
    x.add_argument("--espera", type=int, default=15, help="minutos que o servidor do instalador fica aberto")
    x.set_defaults(f=cmd_instalar)

    a = p.parse_args()
    if getattr(a, "comando", None) and a.comando[:1] == ["--"]:
        a.comando = a.comando[1:]
    a.f(a)


if __name__ == "__main__":
    main()
