#!/usr/bin/env python3
"""Agente da Ponte no PC.

Roda na sessão do usuário no Windows (tarefa agendada no logon), por isso enxerga o NAS
com as credenciais salvas e consegue abrir apps com janela. Recebe jobs do Mac por HTTP
na rede local, mede a carga da máquina e se pausa sozinho quando um jogo está em primeiro
plano. O ícone na bandeja liga e desliga o PC do processo.
"""
import base64
import ctypes
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

try:
    import psutil
except ImportError:  # o instalador põe; sem ele a telemetria fica parcial
    psutil = None

VERSAO = "1.0"
WINDOWS = os.name == "nt"
BASE = Path(os.environ.get("PONTE_DIR") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Ponte")
JOBS_DIR = BASE / "jobs"
AREA = BASE / "area"
LOG = BASE / "agente.log"
PADRAO = {
    "porta": 17850,
    "token": "",
    "nas_unc": "",
    "max_paralelo": 6,
    "max_gpu": 2,
    "auto_pausa_jogos": True,
    # trechos de caminho de executável que indicam jogo em primeiro plano
    "pastas_jogos": [
        "\\steamapps\\common\\", "\\epic games\\", "\\xboxgames\\", "\\riot games\\",
        "\\ea games\\", "\\gog galaxy\\games\\", "\\ubisoft game launcher\\games\\",
        "\\battlestate games\\", "\\call of duty\\", "\\overwatch\\", "\\world of warcraft\\",
    ],
    "jogos_extras": [],  # nomes de exe, ex.: "eldenring.exe"
    # launchers e serviços que moram nas pastas de jogos mas não são jogo: abrir a loja não pausa o PC (24/10/2026,
    # o EpicGamesLauncher.exe pausava o PC como se fosse jogo)
    "excecoes_jogos": [
        "\\epic games\\launcher\\", "\\epic games\\epic online services\\", "\\epic games\\directxredist\\",
        "\\riot games\\riot client\\",
        "epicgameslauncher.exe", "epicgamesupdater.exe", "epicwebhelper.exe", "riotclientservices.exe",
        "riotclientux.exe", "riotclientuxrender.exe",
    ],
}

if WINDOWS:
    BELOW_NORMAL = 0x00004000
    HIGH = 0x00000080
    IDLE = 0x00000040
    NEW_GROUP = 0x00000200
    NO_WINDOW = 0x08000000


def registrar(msg):
    linha = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    try:
        if LOG.exists() and LOG.stat().st_size > 5_000_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linha)
    except OSError:
        pass


def normalizar(texto):
    """Para comparar nomes e caminhos: mesma forma de acento (o Mac manda NFD), sem caixa, barras iguais."""
    return unicodedata.normalize("NFC", str(texto or "")).casefold().replace("\\", "/")


def carregar_config():
    cfg = dict(PADRAO)
    arq = BASE / "config.json"
    if arq.exists():
        # o PowerShell 5 grava UTF-8 com BOM
        cfg.update(json.loads(arq.read_text(encoding="utf-8-sig")))
    return cfg


def salvar_config(cfg):
    (BASE / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- telemetria

class Telemetria:
    def __init__(self):
        self.dados = {}
        self.trava = threading.Lock()

    def gpu(self):
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        campos = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
        try:
            saida = subprocess.run(
                [exe, "--query-gpu=" + campos, "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=NO_WINDOW if WINDOWS else 0,
            ).stdout.strip().splitlines()[0]
        except Exception:
            return None
        v = [x.strip() for x in saida.split(",")]

        def num(s):
            try:
                return float(s)
            except ValueError:
                return None

        return {"nome": v[0], "uso": num(v[1]), "vram_usada_mb": num(v[2]), "vram_total_mb": num(v[3]),
                "temp": num(v[4]), "watts": num(v[5])}

    def ocioso_s(self):
        if not WINDOWS:
            return None

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) // 1000

    def atualizar(self):
        d = {"gpu": self.gpu(), "ocioso_s": self.ocioso_s()}
        if psutil:
            d["cpu"] = psutil.cpu_percent(interval=None)
            d["ram"] = psutil.virtual_memory().percent
            d["ram_livre_gb"] = round(psutil.virtual_memory().available / 2**30, 1)
            d["nucleos"] = psutil.cpu_count()
        try:
            d["disco_livre_gb"] = round(shutil.disk_usage(str(BASE)).free / 2**30, 1)
        except OSError:
            pass
        with self.trava:
            self.dados = d

    def ler(self):
        with self.trava:
            return dict(self.dados)


def processo_em_primeiro_plano():
    """(pid, caminho do exe) da janela em primeiro plano."""
    if not WINDOWS or not psutil:
        return None, ""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, ""
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return pid.value, psutil.Process(pid.value).exe()
    except Exception:
        return pid.value, ""


# ---------------------------------------------------------------- jobs

def encerrar_orfao(j):
    """Job que o agente anterior deixou rodando (Sair na bandeja, queda, atualização): o agente novo não o acompanha
    nem o conta nas vagas, então encerra a árvore. A hora de criação evita matar um PID reaproveitado."""
    if not psutil or not j.get("pid"):
        return
    try:
        raiz = psutil.Process(j["pid"])
        if abs(raiz.create_time() - (j.get("inicio") or 0)) > 10:
            return
        for proc in raiz.children(recursive=True) + [raiz]:
            try:
                proc.kill()
            except Exception:
                pass
        registrar("job órfão encerrado: %s (pid %s)" % (j["id"], j["pid"]))
    except Exception:
        pass


class Jobs:
    def __init__(self, agente):
        self.agente = agente
        self.jobs = {}
        self.procs = {}
        self.envs = {}  # valores do --env só em memória: em job.json e em /jobs vão só os nomes
        self.trava = threading.RLock()
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        # pela hora, não pelo nome: o ID não tem ano e, em janeiro, "1231-..." viria depois de "0102-..."
        for arq in sorted(JOBS_DIR.glob("*/job.json"), key=lambda a: a.stat().st_mtime)[-300:]:
            try:
                j = json.loads(arq.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("estado") == "rodando":
                encerrar_orfao(j)
            if j.get("estado") in ("rodando", "na_fila"):
                j["estado"] = "interrompido"
                j["fim"] = j.get("fim") or time.time()
            self.jobs[j["id"]] = j

    def _gravar(self, j):
        (JOBS_DIR / j["id"] / "job.json").write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")

    def criar(self, pedido):
        jid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        j = {
            "id": jid,
            "nome": pedido.get("nome") or "job",
            "comando": pedido["comando"],
            "shell": pedido.get("shell") or ("pwsh" if WINDOWS else "bash"),
            "cwd": pedido.get("cwd") or str(AREA),
            "env": sorted(pedido.get("env") or {}),
            "gpu": bool(pedido.get("gpu")),
            "estado": "na_fila",
            "criado": time.time(),
            "inicio": None,
            "fim": None,
            "codigo": None,
            "origem": pedido.get("origem") or "",
            "sessao": pedido.get("sessao") or "",  # sessão do app que mandou (vale para a reserva de GPU)
            "dono": pedido.get("dono") or "",
            "venv": pedido.get("venv") or "",
            "mesmo_pausado": bool(pedido.get("mesmo_pausado")),
        }
        (JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
        with self.trava:
            self.envs[jid] = {str(k): str(v) for k, v in (pedido.get("env") or {}).items()}
            self.jobs[jid] = j
            self._gravar(j)
        registrar("novo job %s (%s): %s" % (jid, j["nome"], j["comando"][:200]))
        self.agendar()
        return j

    def _argumentos(self, j):
        cmd = j["comando"]
        if WINDOWS:
            if j["shell"] == "cmd":
                return 'cmd.exe /d /s /c "chcp 65001>nul & %s"' % cmd
            if j["shell"] == "wsl":
                return ["wsl.exe", "-e", "bash", "-lc", cmd]
            prefixo = ("$ProgressPreference='SilentlyContinue';"
                       "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                       "$OutputEncoding=[System.Text.Encoding]::UTF8;")
            codificado = base64.b64encode((prefixo + cmd).encode("utf-16-le")).decode()
            exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
            return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-EncodedCommand", codificado]
        return ["/bin/bash", "-lc", cmd]

    def _iniciar(self, j):
        pasta = JOBS_DIR / j["id"]
        log = open(pasta / "log.txt", "ab")
        env = dict(os.environ)
        env.update(self.envs.pop(j["id"], {}))
        env["PYTHONIOENCODING"] = "utf-8"
        env["PONTE_JOB"] = j["id"]
        env["PONTE_PYTHON"] = str(Path(sys.executable).with_name("python.exe")) if WINDOWS else sys.executable
        env["PONTE_DIR"] = str(BASE)
        if j.get("venv"):
            scripts = Path(j["venv"]) / ("Scripts" if WINDOWS else "bin")
            env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
            env["VIRTUAL_ENV"] = j["venv"]
            env["PYTHONUTF8"] = "1"  # sem isso o Python do Windows lê os JSON do projeto em cp1252 e quebra os acentos
        cwd = j["cwd"]
        if not os.path.isdir(cwd):
            if cwd != str(AREA):
                # pasta pedida que não existe (NAS fora, caminho errado): rodar em outro lugar esconderia o erro
                log.write(("pasta de trabalho não existe: %s\n" % cwd).encode("utf-8"))
                log.close()
                j.update(estado="erro", fim=time.time(), codigo=-1)
                self._gravar(j)
                return
            AREA.mkdir(parents=True, exist_ok=True)
            cwd = str(AREA)
        extra = {}
        if WINDOWS:
            extra["creationflags"] = NEW_GROUP | NO_WINDOW | self.classe_de(j)
        else:
            extra["start_new_session"] = True
        try:
            p = subprocess.Popen(self._argumentos(j), cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, **extra)
        except Exception as e:
            log.write(("falha ao iniciar: %s\n" % e).encode())
            log.close()
            j.update(estado="erro", fim=time.time(), codigo=-1)
            self._gravar(j)
            return
        self.procs[j["id"]] = (p, log)
        j.update(estado="rodando", inicio=time.time(), pid=p.pid)
        self._gravar(j)

    def agendar(self):
        cfg = self.agente.cfg
        with self.trava:
            for jid, (p, log) in list(self.procs.items()):
                codigo = p.poll()
                if codigo is None:
                    continue
                log.close()
                del self.procs[jid]
                j = self.jobs[jid]
                if j["estado"] == "rodando":
                    j["estado"] = "ok" if codigo == 0 else "erro"
                j.update(fim=time.time(), codigo=codigo)
                self._gravar(j)
                registrar("fim job %s: %s (%s)" % (jid, j["estado"], codigo))
            pausado = self.agente.pausado()  # pausado, só sobem os pedidos com --mesmo-pausado (em prioridade IDLE)
            reserva = self.agente.reserva()
            rodando = [self.jobs[k] for k in self.procs]
            gpu = sum(1 for j in rodando if j["gpu"])
            # com a GPU reservada, os jobs do dono passam na frente da fila, ganham vagas além do max_paralelo e
            # são os únicos que entram na GPU; os de GPU dos outros esperam a reserva acabar
            dono = {jid: self.agente.do_dono(j, reserva) for jid, j in self.jobs.items() if j["estado"] == "na_fila"}
            for j in sorted(self.jobs.values(), key=lambda x: (not dono.get(x["id"]), x["criado"])):
                if j["estado"] != "na_fila" or (pausado and not j.get("mesmo_pausado")):
                    continue
                limite = cfg["max_paralelo"] + (cfg["max_gpu"] if dono.get(j["id"]) else 0)
                if len(rodando) >= limite:
                    continue
                if j["gpu"] and (gpu >= cfg["max_gpu"] or (reserva and not dono.get(j["id"]))):
                    continue
                self._iniciar(j)
                if j["estado"] == "rodando":
                    rodando.append(j)
                    gpu += 1 if j["gpu"] else 0

    def cancelar(self, jid):
        with self.trava:
            j = self.jobs.get(jid)
            if not j:
                return None
            if j["estado"] == "na_fila":
                self.envs.pop(jid, None)
                j.update(estado="cancelado", fim=time.time())
                self._gravar(j)
            elif jid in self.procs:
                j["estado"] = "cancelado"
                p = self.procs[jid][0]
                if WINDOWS:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                                   creationflags=NO_WINDOW)
                else:
                    import signal
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except OSError:
                        pass
            return j

    def classe_de(self, j):
        """Classe de prioridade do Windows para o job: pausado, tudo em IDLE; com a GPU reservada, o dono em HIGH,
        os de GPU dos outros em IDLE e o resto dos outros em BELOW_NORMAL; sem reserva, normal (0)."""
        if not WINDOWS:
            return 0
        if self.agente.pausado():
            return IDLE
        reserva = self.agente.reserva()
        if not reserva:
            return 0
        if self.agente.do_dono(j, reserva):
            return HIGH
        return IDLE if j["gpu"] else BELOW_NORMAL

    def reaplicar_prioridades(self):
        """Aplica a classe de cada job ao que já está rodando, filhos incluídos (pausa e reserva mudaram)."""
        if not (WINDOWS and psutil):
            return
        with self.trava:
            for jid, (p, _) in self.procs.items():
                classe = self.classe_de(self.jobs[jid]) or psutil.NORMAL_PRIORITY_CLASS
                try:
                    raiz = psutil.Process(p.pid)
                    for proc in [raiz] + raiz.children(recursive=True):
                        proc.nice(classe)
                except Exception:
                    pass

    def lista(self, limite=50):
        with self.trava:
            return sorted(self.jobs.values(), key=lambda x: x["criado"], reverse=True)[:limite]


# ---------------------------------------------------------------- agente

class Agente:
    def __init__(self):
        BASE.mkdir(parents=True, exist_ok=True)
        AREA.mkdir(parents=True, exist_ok=True)
        self.cfg = carregar_config()
        self.pausa_manual = bool(self.cfg.get("pausa_manual"))
        self.pausa_jogo = None  # {"pid", "exe"}
        # jogo que você liberou retomando à mão: não pausa de novo até ele fechar (sobrevive a reinício)
        self.jogo_liberado = self.cfg.get("jogo_liberado")
        self.tele = Telemetria()
        self.jobs = Jobs(self)
        self.icone = None

    def pausado(self):
        return self.pausa_manual or self.pausa_jogo is not None

    def motivo_pausa(self):
        if self.pausa_manual:
            return "manual"
        if self.pausa_jogo:
            return "jogo: " + os.path.basename(self.pausa_jogo["exe"])
        return None

    def definir_pausa(self, valor):
        antes = self.pausado()
        self.pausa_manual = bool(valor)
        if not valor and self.pausa_jogo:
            self.jogo_liberado = self.pausa_jogo
            registrar("jogo liberado à mão: %s" % os.path.basename(self.pausa_jogo["exe"]))
        if not valor:
            self.pausa_jogo = None
        if self.cfg.get("pausa_manual") != self.pausa_manual or self.cfg.get("jogo_liberado") != self.jogo_liberado:
            self.cfg["pausa_manual"] = self.pausa_manual
            self.cfg["jogo_liberado"] = self.jogo_liberado
            salvar_config(self.cfg)
        self._apos_mudanca(antes)

    def _apos_mudanca(self, antes):
        agora = self.pausado()
        if agora != antes:
            registrar("pausa: %s" % (self.motivo_pausa() or "não"))
            self.jobs.reaplicar_prioridades()
        self.atualizar_icone()

    # ---- reserva de GPU: a GPU só para o trabalho de um projeto ou de algumas sessões (pc reserva-gpu)

    def reserva(self):
        """A reserva em vigor ({dono, marcas, sessoes, desde, ate}) ou None; a vencida sai sozinha."""
        r = self.cfg.get("reserva_gpu")
        if r and r.get("ate") and time.time() > r["ate"]:
            registrar("reserva de GPU de %s venceu" % r.get("dono"))
            self.definir_reserva(None)
            return None
        return r or None

    def definir_reserva(self, r):
        self.cfg["reserva_gpu"] = r
        salvar_config(self.cfg)
        registrar("reserva de GPU: %s" % ("%s (sessões %s, marcas %s)" % (r["dono"], r.get("sessoes"), r.get("marcas"))
                                          if r else "solta"))
        self.jobs.reaplicar_prioridades()
        self.jobs.agendar()
        self.atualizar_icone()

    def do_dono(self, j, reserva=None):
        """O job (ou pedido) é de quem reservou a GPU: mesma sessão do app, mesmo dono, ou cita uma das marcas
        (ex.: o nome do projeto) no comando, na pasta ou no nome."""
        r = reserva if reserva is not None else self.reserva()
        if not r:
            return False
        if j.get("sessao") and j["sessao"] in (r.get("sessoes") or []):
            return True
        if j.get("dono") and normalizar(j["dono"]) == normalizar(r.get("dono")):
            return True
        texto = normalizar(" ".join(str(j.get(k) or "") for k in ("comando", "cwd", "nome")))
        return any(normalizar(m) in texto for m in r.get("marcas") or [] if m)

    def e_jogo(self, exe):
        baixo = exe.lower()
        excecoes = self.cfg.get("excecoes_jogos") or PADRAO["excecoes_jogos"]
        if any(t in baixo for t in excecoes if "\\" in t) or os.path.basename(baixo) in excecoes:
            return False
        if any(t in baixo for t in self.cfg["pastas_jogos"]):
            return True
        return os.path.basename(baixo) in [x.lower() for x in self.cfg["jogos_extras"]]

    def vigiar_jogos(self):
        antes = self.pausado()
        if self.pausa_jogo:
            if not (psutil and psutil.pid_exists(self.pausa_jogo["pid"])):
                self.pausa_jogo = None
        elif self.cfg["auto_pausa_jogos"]:
            if self.jogo_liberado and not (psutil and psutil.pid_exists(self.jogo_liberado["pid"])):
                registrar("jogo liberado fechou: pausa automática volta a valer")
                self.jogo_liberado = None
                self.cfg["jogo_liberado"] = None
                salvar_config(self.cfg)
            pid, exe = processo_em_primeiro_plano()
            liberado = self.jogo_liberado and self.jogo_liberado["pid"] == pid
            if pid and exe and self.e_jogo(exe) and not liberado:
                self.pausa_jogo = {"pid": pid, "exe": exe}
        self._apos_mudanca(antes)

    def laco(self):
        if psutil:
            psutil.cpu_percent(interval=None)
        while True:
            try:
                self.tele.atualizar()
                self.vigiar_jogos()
                self.jobs.agendar()
                self.atualizar_icone()
            except Exception as e:
                registrar("erro no laço: %r" % e)
            time.sleep(2)

    def estado(self):
        jobs = self.jobs.lista(200)
        return {
            "versao": VERSAO,
            "host": os.environ.get("COMPUTERNAME") or os.uname().nodename,
            "pausado": self.pausado(),
            "motivo_pausa": self.motivo_pausa(),
            "auto_pausa_jogos": self.cfg["auto_pausa_jogos"],
            "jogo_liberado": os.path.basename(self.jogo_liberado["exe"]) if self.jogo_liberado else None,
            "rodando": sum(1 for j in jobs if j["estado"] == "rodando" and not j["nome"].endswith("-servidor")),
            "servicos": sum(1 for j in jobs if j["estado"] == "rodando" and j["nome"].endswith("-servidor")),
            "na_fila": sum(1 for j in jobs if j["estado"] == "na_fila"),
            "max_paralelo": self.cfg["max_paralelo"],
            "max_gpu": self.cfg["max_gpu"],
            "reserva_gpu": self._resumo_reserva(jobs),
            "nas_ok": os.path.isdir(self.cfg["nas_unc"]) if self.cfg["nas_unc"] else None,  # None: sem NAS configurado
            "maquina": self.tele.ler(),
            "hora": time.time(),
        }

    def _resumo_reserva(self, jobs):
        r = self.reserva()
        if not r:
            return None
        d = dict(r)
        d["retidos"] = sum(1 for j in jobs if j["estado"] == "na_fila" and j["gpu"] and not self.do_dono(j, r))
        d["do_dono_rodando"] = sum(1 for j in jobs if j["estado"] == "rodando" and self.do_dono(j, r))
        d["outros_na_gpu"] = sum(1 for j in jobs if j["estado"] == "rodando" and j["gpu"] and not self.do_dono(j, r))
        return d

    # ---- bandeja

    def imagem_icone(self):
        from PIL import Image, ImageDraw
        cor = (230, 170, 30) if self.pausado() else ((40, 130, 240) if self.jobs.procs else (40, 190, 90))
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=cor)
        return img

    def titulo_icone(self):
        if self.pausado():
            return "Ponte pausada (%s)" % self.motivo_pausa()
        n = len(self.jobs.procs)
        r = self.reserva()
        return "Ponte ativa, %d job%s rodando%s" % (n, "" if n == 1 else "s",
                                                     ", GPU reservada para %s" % r["dono"] if r else "")

    def atualizar_icone(self):
        if not self.icone:
            return
        try:
            self.icone.icon = self.imagem_icone()
            self.icone.title = self.titulo_icone()
            self.icone.update_menu()
        except Exception:
            pass

    def bandeja(self):
        import pystray

        def alternar(icon, item):
            self.definir_pausa(not self.pausado())

        def alternar_auto(icon, item):
            self.cfg["auto_pausa_jogos"] = not self.cfg["auto_pausa_jogos"]
            salvar_config(self.cfg)

        def parar_tudo(icon, item):
            self.definir_pausa(True)
            for j in self.jobs.lista(200):
                if j["estado"] in ("rodando", "na_fila"):
                    self.jobs.cancelar(j["id"])

        def abrir_pasta(icon, item):
            os.startfile(str(BASE))

        def sair(icon, item):
            icon.stop()
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self.titulo_icone(), None, enabled=False),
            pystray.MenuItem(lambda item: "Retomar" if self.pausado() else "Pausar (jobs atuais seguem devagar)",
                             alternar),
            pystray.MenuItem("Pausar e parar tudo", parar_tudo),
            pystray.MenuItem("Pausar sozinho quando abrir jogo", alternar_auto,
                             checked=lambda item: self.cfg["auto_pausa_jogos"]),
            pystray.MenuItem("Abrir pasta da Ponte", abrir_pasta),
            pystray.MenuItem("Sair", sair),
        )
        self.icone = pystray.Icon("ponte", self.imagem_icone(), self.titulo_icone(), menu)
        self.icone.run()


# ---------------------------------------------------------------- HTTP

def criar_handler(agente):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, codigo, dados):
            corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _autorizado(self):
            token = agente.cfg["token"]
            if not token or not hmac.compare_digest(self.headers.get("X-Ponte-Token") or "", token):
                self._json(401, {"erro": "token inválido"})
                return False
            return True

        def _corpo(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        def _caminho(self, q):
            bruto = (q.get("caminho") or [""])[0]
            p = Path(bruto)
            return p if p.is_absolute() or bruto.startswith("\\\\") else AREA / bruto

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            partes = [p for p in url.path.split("/") if p]
            if partes == ["saude"]:
                d = {"ok": True, "ponte": VERSAO, "host": agente.estado()["host"],
                     "pausado": agente.pausado(), "motivo_pausa": agente.motivo_pausa()}
                nonce = (q.get("n") or [""])[0]
                if nonce and agente.cfg["token"]:
                    # prova de que este é o agente certo, sem o token sair daqui: o Mac só confia em quem acertar
                    d["prova"] = hmac.new(agente.cfg["token"].encode(), nonce.encode(), hashlib.sha256).hexdigest()
                return self._json(200, d)
            if not self._autorizado():
                return
            if partes == ["estado"]:
                return self._json(200, agente.estado())
            if partes == ["jobs"]:
                return self._json(200, agente.jobs.lista(int((q.get("limite") or ["50"])[0])))
            if len(partes) == 2 and partes[0] == "jobs":
                j = agente.jobs.jobs.get(partes[1])
                return self._json(200, j) if j else self._json(404, {"erro": "job não existe"})
            if len(partes) == 3 and partes[0] == "jobs" and partes[2] == "log":
                arq = JOBS_DIR / partes[1] / "log.txt"
                desde = int((q.get("desde") or ["0"])[0])
                # estado antes do log: se o job acabar entre as duas leituras, o Mac ainda pede o fim do log
                j = dict(agente.jobs.jobs.get(partes[1]) or {})
                dados = b""
                if arq.exists():
                    with open(arq, "rb") as f:
                        f.seek(desde)
                        dados = f.read(2_000_000)
                return self._json(200, {"texto": dados.decode("utf-8", "replace"), "proximo": desde + len(dados),
                                        "estado": j.get("estado"), "codigo": j.get("codigo")})
            if partes == ["arquivos"]:
                arq = self._caminho(q)
                if arq.is_dir():
                    itens = [{"nome": x.name, "pasta": x.is_dir(), "bytes": x.stat().st_size if x.is_file() else None}
                             for x in sorted(arq.iterdir())]
                    return self._json(200, itens)
                if not arq.is_file():
                    return self._json(404, {"erro": "não existe: %s" % arq})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(arq.stat().st_size))
                self.end_headers()
                with open(arq, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 4 << 20)
                return
            if partes == ["tela"]:
                try:
                    from PIL import ImageGrab
                    img = ImageGrab.grab(all_screens=True)
                except Exception as e:
                    return self._json(500, {"erro": "sem captura de tela: %s" % e})
                buf = io.BytesIO()
                img.save(buf, "PNG")
                corpo = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                return
            self._json(404, {"erro": "rota desconhecida"})

        def do_POST(self):
            partes = [p for p in urlparse(self.path).path.split("/") if p]
            if not self._autorizado():
                return
            if partes == ["jobs"]:
                pedido = self._corpo()
                if not pedido.get("comando"):
                    return self._json(400, {"erro": "faltou o comando"})
                if agente.pausado() and not pedido.get("mesmo_pausado"):
                    return self._json(409, {"erro": "PC pausado", "motivo": agente.motivo_pausa()})
                return self._json(201, agente.jobs.criar(pedido))
            if partes == ["whisper"]:
                # repassa o áudio ao servidor whisper residente (127.0.0.1:17851, venv próprio)
                n = int(self.headers.get("Content-Length") or 0)
                dados = self.rfile.read(n)  # lido antes de qualquer recusa, para a conexão não ficar com lixo
                if agente.pausado():
                    return self._json(409, {"erro": "PC pausado", "motivo": agente.motivo_pausa()})
                consulta = urlparse(self.path).query
                q = {k: v[0] for k, v in parse_qs(consulta).items()}
                r = agente.reserva()
                if r and not agente.do_dono({"sessao": q.get("sessao"), "dono": q.get("dono"),
                                             "nome": q.get("arquivo")}, r):
                    # o ponte_whisper cai sozinho no MLX do Mac
                    return self._json(409, {"erro": "GPU do PC reservada para %s" % r["dono"]})
                req = urllib.request.Request("http://127.0.0.1:17851/transcrever?" + consulta, data=dados, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=1800) as r:
                        corpo = r.read()
                except urllib.error.HTTPError as e:
                    corpo = e.read()
                    return self._json(e.code, json.loads(corpo or b"{}"))
                except OSError:
                    return self._json(503, {"erro": "servidor whisper parado"})
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                return
            if len(partes) == 3 and partes[0] == "jobs" and partes[2] == "cancelar":
                j = agente.jobs.cancelar(partes[1])
                return self._json(200, j) if j else self._json(404, {"erro": "job não existe"})
            if partes == ["pausa"]:
                agente.definir_pausa(self._corpo().get("pausado", True))
                return self._json(200, agente.estado())
            if partes == ["reserva"]:
                d = self._corpo()
                if d.get("soltar"):
                    agente.definir_reserva(None)
                    return self._json(200, {"reserva_gpu": None})
                if not d.get("dono"):
                    return self._json(400, {"erro": "faltou o dono da reserva"})
                horas = float(d.get("horas") or 0)
                agente.definir_reserva({
                    "dono": d["dono"],
                    "marcas": [m for m in d.get("marcas") or [] if m],
                    "sessoes": [s for s in d.get("sessoes") or [] if s],
                    "desde": time.time(),
                    "ate": time.time() + horas * 3600 if horas > 0 else None,
                })
                return self._json(200, {"reserva_gpu": agente.estado()["reserva_gpu"]})
            self._json(404, {"erro": "rota desconhecida"})

        def do_PUT(self):
            try:
                self._put()
            except Exception as e:
                registrar("erro no PUT: %r" % e)
                try:
                    self._json(500, {"erro": str(e)})
                except Exception:
                    pass

        def _put(self):
            if not self._autorizado():
                return
            url = urlparse(self.path)
            if url.path.rstrip("/") != "/arquivos":
                return self._json(404, {"erro": "rota desconhecida"})
            arq = self._caminho(parse_qs(url.query))
            arq.parent.mkdir(parents=True, exist_ok=True)
            restante = int(self.headers.get("Content-Length") or 0)
            temp = arq.with_name("%s.%s.parcial" % (arq.name, uuid.uuid4().hex[:8]))  # dois envios do mesmo arquivo não colidem
            with open(temp, "wb") as f:
                while restante > 0:
                    bloco = self.rfile.read(min(restante, 4 << 20))
                    if not bloco:
                        break
                    f.write(bloco)
                    restante -= len(bloco)
            if restante:
                temp.unlink(missing_ok=True)
                return self._json(400, {"erro": "envio incompleto"})
            os.replace(temp, arq)
            mt = parse_qs(url.query).get("mtime")
            if mt:
                os.utime(arq, (float(mt[0]), float(mt[0])))
            self._json(201, {"caminho": str(arq), "bytes": arq.stat().st_size})

    return Handler


def main():
    agente = Agente()
    if not agente.cfg.get("token"):
        # sem token qualquer aparelho da rede mandaria comandos para este PC: o agente não sobe
        registrar("sem token em config.json: agente não iniciado (rode pc instalar de novo)")
        sys.exit(1)
    try:
        servidor = ThreadingHTTPServer(("0.0.0.0", int(agente.cfg["porta"])), criar_handler(agente))
    except OSError as e:
        registrar("porta ocupada, outro agente já roda? %s" % e)
        sys.exit(0)
    servidor.daemon_threads = True
    registrar("agente %s no ar na porta %s" % (VERSAO, agente.cfg["porta"]))
    threading.Thread(target=agente.laco, daemon=True).start()
    sem_bandeja = "--sem-bandeja" in sys.argv or not WINDOWS
    if sem_bandeja:
        servidor.serve_forever()
        return
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        agente.bandeja()
    except Exception as e:
        registrar("sem bandeja (%r), seguindo sem ícone" % e)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
