#!/usr/bin/env python3
"""Instalador da Ponte no Mac: pergunta sobre o seu setup, grava a configuração e instala as ferramentas.

    python3 install.py                       pergunta tudo no terminal
    python3 install.py --avancado            inclui as perguntas avançadas (porta, faixa de rede, winget extra)
    python3 install.py --respostas r.json    não pergunta: usa as respostas do arquivo (é como a skill chama)
    python3 install.py --perguntas           imprime as perguntas em JSON (para quem automatiza)
    python3 install.py --simular             mostra o que faria, sem escrever nada
    python3 install.py --desinstalar         tira o `pc` e a skill (não mexe no PC)

O que faz no Mac, e só isso:
  1. cria ~/.config/ponte/config.json (mantém o token se já existir)
  2. copia ponte/ para ~/.local/share/ponte e liga ~/.local/bin/pc
  3. instala a skill em ~/.claude/skills/ponte-pc (guarda a antiga ao lado, se houver)
  4. garante a chave ssh (id_ed25519) e confere ffmpeg/ffprobe
Nada é instalado no PC por aqui: isso é o `pc instalar`, que você roda no fim.
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
HOME = Path.home()
CONFIG = Path(os.environ.get("PONTE_CONFIG") or HOME / ".config" / "ponte" / "config.json")
DESTINO = HOME / ".local" / "share" / "ponte"
BIN = HOME / ".local" / "bin"
SKILL = HOME / ".claude" / "skills" / "ponte-pc"


def rodar(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ------------------------------------------------------------ detecção

def detectar_faixa():
    """Faixa /24 da interface padrão do Mac (o chute mais comum para rede de casa)."""
    m = re.search(r"interface: (\S+)", rodar(["route", "-n", "get", "default"]))
    ip = rodar(["ipconfig", "getifaddr", m.group(1)]).strip() if m else ""
    try:
        return str(ipaddress.ip_network(ip + "/24", strict=False))
    except ValueError:
        return "192.168.0.0/24"


def detectar(chave, respostas):
    if chave == "faixa_rede":
        return detectar_faixa()
    if chave == "nas_mac":
        return "/Volumes/" + (respostas.get("nas_share") or "NAS")
    return ""


# ------------------------------------------------------------ perguntas

def aplica(p, respostas):
    return all(respostas.get(k) == v or (isinstance(v, list) and respostas.get(k) in v)
               for k, v in (p.get("so_se") or {}).items())


def padrao_de(p, respostas):
    pad = p.get("padrao")
    if isinstance(pad, str) and pad.startswith("@detectar:"):
        return detectar(pad.split(":", 1)[1], respostas)
    return pad


def perguntar(p, respostas):
    pad = padrao_de(p, respostas)
    print("\n" + p["pergunta"])
    if p.get("ajuda"):
        print("  " + p["ajuda"])
    tipo = p["tipo"]
    if tipo == "escolha":
        opcoes = p["opcoes"]
        for i, o in enumerate(opcoes, 1):
            marca = " (padrão)" if o["valor"] == pad else ""
            print("  %d) %s%s%s" % (i, o["rotulo"], marca, ("  - " + o["descricao"]) if o.get("descricao") else ""))
        while True:
            r = input("> ").strip()
            if not r and pad is not None:
                return pad
            if r.isdigit() and 1 <= int(r) <= len(opcoes):
                return opcoes[int(r) - 1]["valor"]
            print("  digite o número da opção")
    if tipo == "multi":
        opcoes = p["opcoes"]
        for i, o in enumerate(opcoes, 1):
            marca = "*" if o["valor"] in (pad or []) else " "
            print("  %d) [%s] %s%s" % (i, marca, o["rotulo"], ("  - " + o["descricao"]) if o.get("descricao") else ""))
        while True:
            r = input("números separados por vírgula (vazio = os marcados com *, 0 = nenhum)> ").strip()
            if not r:
                return list(pad or [])
            if r == "0":
                return []
            try:
                idx = [int(x) for x in r.replace(" ", "").split(",") if x]
                if all(1 <= i <= len(opcoes) for i in idx):
                    return [opcoes[i - 1]["valor"] for i in idx]
            except ValueError:
                pass
            print("  exemplo: 1,2,4")
    if tipo == "numero":
        while True:
            r = input("[%s]> " % pad).strip()
            try:
                n = int(r) if r else pad
                if p.get("minimo", n) <= n <= p.get("maximo", n):
                    return n
            except (ValueError, TypeError):
                pass
            print("  número entre %s e %s" % (p.get("minimo"), p.get("maximo")))
    ex = "  ex.: %s" % p["exemplo"] if p.get("exemplo") else ""
    r = input("[%s]%s> " % (pad if pad is not None else "", ex)).strip()
    return r or pad or ""


def coletar(perguntas, arquivo, avancado):
    respostas = {}
    dados = json.loads(Path(arquivo).read_text(encoding="utf-8")) if arquivo else None
    for p in perguntas:
        if not aplica(p, respostas):
            continue
        if dados is not None:
            v = dados.get(p["id"], padrao_de(p, respostas))
            if v is None and p["tipo"] != "multi":
                sys.exit("faltou a resposta '%s' em %s" % (p["id"], arquivo))
            respostas[p["id"]] = v if v is not None else []
            continue
        if p.get("avancado") and not avancado:
            respostas[p["id"]] = padrao_de(p, respostas)
            continue
        respostas[p["id"]] = perguntar(p, respostas)
    return respostas


def validar(r):
    """O que o instalador ainda não sabe fazer é dito aqui, antes de mexer em qualquer coisa."""
    if r["so_principal"] != "macos":
        sys.exit("Por enquanto o computador principal precisa ser um Mac (o controlador usa top, ioreg, route, "
                 "networksetup). Veja o Roteiro no README; contribuições são bem-vindas.")
    if r["so_trabalho"] != "windows":
        sys.exit("Por enquanto a máquina de trabalho precisa ser um PC com Windows 10/11 (o agente e o instalador são "
                 "PowerShell/winget). Veja o Roteiro no README.")
    if "transcricao" in r["modulos"] and r["gpu"] != "nvidia":
        print("aviso: a transcrição do PC usa CUDA; sem GPU NVIDIA ela fica desligada.", file=sys.stderr)
        r["modulos"] = [m for m in r["modulos"] if m != "transcricao"]
    if r["nas"] == "smb":
        for k in ("nas_host", "nas_share", "nas_mac"):
            if not r.get(k):
                sys.exit("NAS marcado, mas faltou '%s'" % k)
        r["nas_mac"] = r["nas_mac"].rstrip("/")
        if not r["nas_mac"].startswith("/"):
            sys.exit("nas_mac precisa ser um caminho absoluto do Mac, ex.: /Volumes/Projetos")
    try:
        ipaddress.ip_network(r["faixa_rede"], strict=False)
    except ValueError:
        sys.exit("faixa de rede inválida: %s (ex.: 192.168.1.0/24)" % r["faixa_rede"])
    if r["teto_mac_ausente"] < r["teto_mac"]:
        r["teto_mac_ausente"] = r["teto_mac"]
    return r


# ------------------------------------------------------------ montagem da config

def montar_config(r, antiga):
    pacotes = list(r["pacotes"])
    extras = [x.strip() for x in (r.get("winget_extra") or "").split(",") if x.strip()]
    if r["acesso"] == "tailscale":
        extras.append("tailscale.tailscale")
    if "transcricao" in r["modulos"]:
        for obrigatorio in ("Python.Python.3.12", "astral-sh.uv", "Gyan.FFmpeg"):
            if obrigatorio not in pacotes:
                pacotes.append(obrigatorio)
    c = dict(antiga)
    c.update({
        "porta": r["porta"],
        "perfil": r["gpu"],
        "rede": r["faixa_rede"],
        "teto_mac": r["teto_mac"],
        "teto_mac_ausente": r["teto_mac_ausente"],
        "tailscale": r["acesso"] == "tailscale",
        "nas_host": r.get("nas_host", "") if r["nas"] == "smb" else "",
        "nas_share": r.get("nas_share", "") if r["nas"] == "smb" else "",
        "nas_mac": r.get("nas_mac", "") if r["nas"] == "smb" else "",
        "instalar": {
            "pacotes": pacotes + [e for e in extras if e not in pacotes],
            "nas": r["nas"] == "smb",
            "claude_code": "claude_code" in r["modulos"],
            "whisper": "transcricao" in r["modulos"],
            "pausa_jogos": "pausa_jogos" in r["modulos"],
        },
    })
    c["repo"] = str(RAIZ)
    c.setdefault("token", "")
    c.setdefault("host", "")
    c.setdefault("usuario", "")
    return c


# ------------------------------------------------------------ ações

def gravar_config(c, simular):
    print("config -> %s" % CONFIG)
    if simular:
        print(json.dumps({k: v for k, v in c.items() if k != "token"}, indent=2, ensure_ascii=False))
        return
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(CONFIG, 0o600)


def copiar_ferramentas(simular, dev):
    print("ferramentas -> %s%s" % (DESTINO, " (link para o repositório)" if dev else ""))
    if simular:
        return
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    if dev:
        if DESTINO.is_symlink() or DESTINO.exists():
            if DESTINO.is_symlink():
                DESTINO.unlink()
            else:
                sys.exit("%s já existe e não é um link; apague ou mova antes de usar --dev" % DESTINO)
        DESTINO.symlink_to(RAIZ / "ponte")
    else:
        if DESTINO.is_symlink():
            DESTINO.unlink()
        # preserva o que a Ponte guarda ali (jobs, medidas de rede) e troca só o código
        DESTINO.mkdir(parents=True, exist_ok=True)
        for item in (RAIZ / "ponte").iterdir():
            if item.name == "__pycache__":
                continue
            alvo = DESTINO / item.name
            if item.is_dir():
                shutil.rmtree(alvo, ignore_errors=True)
                shutil.copytree(item, alvo, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(item, alvo)
    pc = DESTINO / "pc.py"
    os.chmod(pc, 0o755)
    BIN.mkdir(parents=True, exist_ok=True)
    link = BIN / "pc"
    if link.is_symlink() or link.exists():
        if link.is_symlink() and os.path.realpath(link) != os.path.realpath(pc):
            print("  aviso: %s já aponta para %s; troquei" % (link, os.path.realpath(link)))
        link.unlink()
    link.symlink_to(pc)
    if str(BIN) not in os.environ.get("PATH", "").split(":"):
        print("  coloque %s no PATH:  echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc" % BIN)


def instalar_skill(simular):
    print("skill -> %s" % SKILL)
    if simular:
        return
    origem = RAIZ / "skill" / "ponte-pc"
    if SKILL.exists():
        copia = SKILL.with_name("ponte-pc.antes-%s" % time.strftime("%Y%m%d-%H%M%S"))
        print("  já existia uma skill ponte-pc; guardada em %s" % copia)
        SKILL.rename(copia)
    shutil.copytree(origem, SKILL)


def garantir_chave(simular):
    chave = HOME / ".ssh" / "id_ed25519"
    if chave.exists() and chave.with_suffix(".pub").exists():
        return
    print("chave ssh: criando %s (sem senha, só para falar com o seu PC)" % chave)
    if simular:
        return
    (HOME / ".ssh").mkdir(mode=0o700, exist_ok=True)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "ponte", "-f", str(chave)], check=True)


def conferir_mac():
    faltam = [x for x in ("ffmpeg", "ffprobe", "ssh", "scp", "curl") if not shutil.which(x)]
    if faltam:
        print("faltam no Mac: %s" % ", ".join(faltam))
        if shutil.which("brew") and any(x.startswith("ff") for x in faltam):
            print("  instale com:  brew install ffmpeg")
        elif any(x.startswith("ff") for x in faltam):
            print("  instale o Homebrew (https://brew.sh) e depois:  brew install ffmpeg")


def desinstalar():
    for alvo in (BIN / "pc",):
        if alvo.is_symlink():
            alvo.unlink()
            print("removido %s" % alvo)
    if SKILL.exists():
        shutil.rmtree(SKILL)
        print("removida %s" % SKILL)
    print("ficaram: %s (config e token) e %s (código e jobs). Apague à mão se quiser; no PC, "
          "desinstale a tarefa agendada 'Ponte Agente' e a pasta %%LOCALAPPDATA%%\\Ponte." % (CONFIG.parent, DESTINO))


def main():
    ap = argparse.ArgumentParser(description="Instalador da Ponte no Mac")
    ap.add_argument("--respostas", help="arquivo JSON com as respostas (não pergunta nada)")
    ap.add_argument("--avancado", action="store_true", help="faz também as perguntas avançadas")
    ap.add_argument("--perguntas", action="store_true", help="imprime as perguntas em JSON e sai")
    ap.add_argument("--simular", action="store_true", help="mostra o que faria, sem escrever nada")
    ap.add_argument("--dev", action="store_true", help="liga ~/.local/share/ponte ao repositório (para quem edita o código)")
    ap.add_argument("--desinstalar", action="store_true")
    a = ap.parse_args()

    perguntas = json.loads((RAIZ / "perguntas.json").read_text(encoding="utf-8"))["perguntas"]
    if a.perguntas:
        print((RAIZ / "perguntas.json").read_text(encoding="utf-8"))
        return
    if a.desinstalar:
        return desinstalar()
    if sys.platform != "darwin":
        sys.exit("O install.py roda no Mac (computador principal). No PC, quem instala é o `pc instalar`, de lá do Mac.")

    print("Ponte: vou fazer algumas perguntas sobre o seu setup. Enter aceita o padrão.")
    r = validar(coletar(perguntas, a.respostas, a.avancado))
    antiga = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    c = montar_config(r, antiga)

    print()
    gravar_config(c, a.simular)
    copiar_ferramentas(a.simular, a.dev)
    if r["instalar_skill"] == "sim":
        instalar_skill(a.simular)
    garantir_chave(a.simular)
    conferir_mac()

    print("\nPronto no Mac. Falta o PC:")
    print("  pc instalar        # mostra uma linha para colar no PowerShell (Administrador) do PC e espera ele avisar")
    print("  pc doctor          # confere tudo depois")
    print("  pc onde encode --min 5")
    if r["instalar_agora"] == "sim" and not a.simular:
        print("\nAbrindo o instalador do PC (Ctrl+C para sair)...\n")
        os.execv(str(BIN / "pc"), [str(BIN / "pc"), "instalar"])


if __name__ == "__main__":
    main()
