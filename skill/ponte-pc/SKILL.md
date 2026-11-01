---
name: ponte-pc
description: Decide em qual máquina rodar trabalho pesado (este Mac ou o PC com Windows ligado à Ponte) e delega para o PC. Use ANTES de qualquer render, encode, conversão em lote, transcrição, geração de imagem ou vídeo por IA, modelo local ou build pesado, e sempre que o usuário falar em PC, Ponte ou em delegar tarefa para o outro computador. Também use para INSTALAR ou CONFIGURAR a Ponte pela primeira vez ("instala a ponte", "configura a ponte-pc"): faz uma entrevista sobre o setup do usuário e roda o instalador.
---

# Ponte Mac ↔ PC

O Mac é o computador do dia a dia e deve ficar sempre utilizável. O PC (Windows, de preferência com GPU NVIDIA) fica
ligado para absorver carga. Você decide sozinho onde cada coisa roda e conta em uma linha o que decidiu.

Nada aqui é fixo: máquinas, rede, NAS e tetos moram em `~/.config/ponte/config.json` (veja `pc config`). **Antes de
supor qualquer coisa sobre o setup, rode `pc config` e `pc estado`.**

## Primeiro: a Ponte já está instalada?

```bash
pc config      # se o comando não existe, ou o token está vazio, ou o host está vazio: falta instalar
pc doctor      # o que falta, no Mac e no PC
```

Se `pc` não existe ou o `doctor` aponta falta de configuração, siga **"Instalar e configurar (entrevista)"** abaixo.
Se está tudo `ok`, pule para **"Uso"**.

## Instalar e configurar (entrevista)

A instalação **pergunta** sobre o computador de quem usa. As perguntas ficam em `perguntas.json` no repositório da
Ponte (a mesma lista que o `install.py` faz no terminal). O repositório é
`https://github.com/rolandinhobr/ponte-pc`; se ele não estiver no disco, clone com
`git clone https://github.com/rolandinhobr/ponte-pc.git ~/Developer/ponte-pc` (ou pergunte onde a pessoa quer). Se já
houver uma config, `pc config` mostra o caminho em `repo`.

1. **Leia** `perguntas.json` (ou `python3 <repo>/install.py --perguntas`).
2. **Pergunte com `AskUserQuestion`**, em grupos de até 4 perguntas por chamada, na ordem do arquivo. Use `rotulo` e
   `descricao` das opções; `tipo: multi` vira `multiSelect: true`; `texto` e `numero` viram a opção "Other" com o
   padrão sugerido na pergunta. Regras:
   - pule as de `"avancado": true` (porta, faixa de rede, winget extra), a menos que a pessoa peça opções avançadas;
   - pule as com `so_se` que não se aplicam (ex.: dados do NAS quando respondeu "não tenho NAS");
   - valores `@detectar:...` são sugestões que o `install.py` calcula; se precisar de um valor para mostrar, rode
     `python3 <repo>/install.py --simular --respostas <arquivo>` e leia a config proposta;
   - **se a pessoa disser que o computador principal não é Mac ou que o de trabalho não é Windows, pare e explique**:
     por enquanto só Mac → Windows é suportado (veja "Roteiro" no README). Não improvise outro caminho.
3. **Grave as respostas** em um JSON temporário (`{"so_principal": "macos", "gpu": "nvidia", ...}`: chave = `id`,
   valor = `valor` da opção, lista para `multi`) e rode primeiro em modo de simulação:
   ```bash
   python3 <repo>/install.py --respostas /tmp/ponte-respostas.json --simular
   ```
   Mostre à pessoa, em poucas linhas, o que vai ser configurado e instalado (pacotes do PC inclusive) e peça o OK.
4. **Instale de verdade** (sem `--simular`). Isso só mexe no Mac: config, `~/.local/bin/pc`, a skill, a chave ssh.
5. **O PC é com a pessoa**: o instalador dele precisa de PowerShell **como administrador**, diante do PC. Rode
   `pc instalar` com `run_in_background`, leia a linha `irm http://.../instalar.ps1 | iex` que ele imprime, entregue-a à
   pessoa e explique: abrir "Terminal (Admin)" no PC, colar, esperar. O comando espera o PC avisar (15 min). Se
   pedir login no NAS, ela digita no PC. **Nunca peça nem digite a senha do NAS ou do Windows por ela.**
6. Quando o PC avisar, rode `pc doctor` e `pc estado`. Se algo estiver `FALTA`, resolva com a dica que ele dá.
7. **Calibre** (uma vez, 5 minutos): rode uma tarefa real dos dois lados e compare, ajustando `velocidade` no
   `config.json` (veja "Personalizar"). A tabela padrão é de outra máquina e serve só de ponto de partida.

Atualizar depois de um `git pull` no repositório: rode `python3 <repo>/install.py --respostas ...` de novo (o token e o
host ficam) e `pc atualiza` para mandar o agente novo ao PC.

## Uso

### Política

As duas máquinas trabalham ao mesmo tempo; nunca uma fica parada enquanto há trabalho para a outra. O único freio é a
**folga do Mac**: `teto_mac` (com a pessoa usando) e `teto_mac_ausente` (longe há 10 min), em %. **Todo comando
pesado que rodar no Mac usa `nice -n 10`.** O PC pega trabalho até `pc_cheio` (95%) de CPU/GPU, com
`pc_vram_min_mb` de VRAM livre. PC pausado (modo jogo ou `pc pausa`) ou fora do ar fica de fora; **não retome sem a
pessoa pedir**. Quem aplica as regras é o `pc onde`: não refaça a conta de cabeça.

### Fluxo para cada tarefa pesada

1. `pc onde <tipo> [--min <duração estimada>] [--entrada <arquivo>]... [--lote N]`. Passe sempre `--entrada`: com ela,
   `encode`/`render` de origem 4:2:2 10-bit vira `422` e de origem 4K vira `4k` sozinho, e o custo de enviar o arquivo
   entra na conta.
2. **PC**: `pc roda --nome x [--gpu] [--espera] -- '<comando em PowerShell>'`. **MAC**: rode aqui, com `nice -n 10`
   quando ele disser "com nice". **LOTE**: a parte do Mac e a do PC rodam ao mesmo tempo. **ESPERAR**: só quando as
   duas estão cheias.
3. **Confira a saída** (ffprobe, tamanho, log) antes de dizer que está pronto.

Tipos: `leve`, `encode`, `prores`, `422`, `4k`, `av1`, `transcricao`, `cuda`, `ia-imagem`, `ia-video`, `llm`,
`llm-grande`, `gui`, `render`, `cpu`, `build` (o que cada um prefere: `pc onde --help` e `TIPOS` em `pc.py`).
Com o perfil `sem-gpu`, o que exige GPU fica no Mac.

### Comandos

```bash
pc estado                      # carga das duas máquinas, pausa, jobs
pc onde encode --min 5 --entrada "/Volumes/MeuNAS/clipe.mov"
pc roda --nome x --gpu --espera -- 'ffmpeg -hwaccel cuda -i "/Volumes/MeuNAS/a.mov" -c:v hevc_nvenc -preset p5 "/Volumes/MeuNAS/b.mp4"'
pc roda --nome x -- '...'      # sem --espera devolve o id na hora
pc espera <id> | pc log <id> [-f] | pc jobs | pc cancela <id>
pc envia <arquivo do Mac> [destino no PC] | pc traz <caminho no PC> [local]
pc tela [arquivo.png]          # captura a tela do PC
pc caminho <caminho>           # traduz entre Mac e Windows (precisa do NAS configurado)
pc reserva-gpu --dono "PROJETO" --horas 72 | pc reserva-gpu --soltar   # GPU só para um projeto (só se a pessoa pedir)
pc pausa | pc retoma           # só se a pessoa pedir
pc config | pc doctor | pc atualiza | pc instalar
```

- O comando roda no **PowerShell 7** do PC (`--shell cmd` ou `--shell wsl` se precisar). Use aspas simples em volta do
  comando inteiro no zsh. No FFmpeg do Windows, a saída nula é `-f null NUL`.
- **Caminhos do NAS no formato do Mac, entre aspas, são traduzidos sozinhos** para o caminho de rede do Windows
  (só com `nas_*` configurado). Pasta de trabalho padrão no PC: `%LOCALAPPDATA%\Ponte\area`, no disco local; para ler
  muito de um NAS, copie para lá primeiro.
- Job longo: lance sem `--espera` e rode `pc espera <id>` com `run_in_background`. Não fique checando em loop.
- `--gpu` ocupa uma das `max_gpu` vagas de GPU do PC (2 por padrão); o seguinte espera na fila. Cada
  PC aceita `max_paralelo` jobs (6).
- Pode instalar o que faltar no PC com `pc roda` (winget, pip, uv). O agente roda na sessão do usuário do Windows,
  com as permissões dele (e UAC pode pedir confirmação na tela do PC; veja `pc tela`).
- Em NAS grande nunca liste sem limite: `Get-ChildItem -Depth 3` no PC e `find -maxdepth 3` no Mac.

### Receitas de GPU no PC (FFmpeg, GPU NVIDIA)

- H.264/HEVC/AV1: `-hwaccel cuda -i ... -c:v hevc_nvenc -preset p5 -b:v 30M -pix_fmt p010le` (`h264_nvenc`, `av1_nvenc`).
- ProRes (CPU): `-c:v prores_aw -profile:v 2` (422) ou `-profile:v 3` (HQ).
- O maior ganho é **paralelismo**: vários itens de lote ao mesmo tempo (vários `pc roda`) usam a máquina inteira.
- A placa tem limite de sessões NVENC simultâneas (varia por modelo e driver; outros programas contam). Se aparecer
  "Error while opening encoder" com muitos encodes juntos, diminua o paralelismo.

### Transcrição (se instalada)

`ponte_whisper.py` (em `~/.local/share/ponte`) é um substituto direto do `mlx_whisper.transcribe`: usa o PC quando ele
está livre e cai no MLX do Mac se estiver pausado, fora do ar ou falhar. No projeto:
`import os, sys; sys.path.insert(0, os.path.expanduser("~/.local/share/ponte")); import ponte_whisper as mlx_whisper`.
`PONTE_WHISPER=mac` força o Mac; `PONTE_WHISPER=pc` falha em vez de cair no Mac.

### Fora de casa (se Tailscale)

Com `tailscale: true`, o `pc` e o `ssh pc` acham o PC pelo Tailscale sozinhos fora da rede de casa; `pc estado` mostra
"[pelo Tailscale: Mac fora de casa]". O Tailscale precisa estar ligado nas duas máquinas. Sem o NAS montado, o `pc onde`
manda para o PC o que lê do NAS. Arquivos do disco do Mac saem pela internet de onde ele estiver: o `pc onde` mede essa
velocidade (a cada 6 h por rede) e pesa o envio contra a duração da tarefa (passe `--min` e `--entrada`).

## Personalizar

Tudo em `~/.config/ponte/config.json` (o `install.py` cria; edite à vontade, `pc config` mostra):

| chave | o que é |
|---|---|
| `perfil` | `nvidia` ou `sem-gpu`: escolhe a tabela de tipos/velocidades |
| `velocidade` | `{"encode": [pc, mac]}`: velocidade relativa por tipo; vale por cima da tabela padrão |
| `teto_mac`, `teto_mac_ausente` | % de CPU/GPU que o Mac aceita fazendo tarefa com você usando / longe |
| `pc_cheio`, `pc_vram_min_mb` | quando o PC deixa de aceitar |
| `nas_host`, `nas_share`, `nas_mac` | NAS e onde ele monta no Mac (vazio = sem tradução de caminho) |
| `rede`, `porta`, `tailscale` | faixa de procura do PC, porta do agente, uso do Tailscale |
| `instalar` | o que o `pc instalar` põe no PC (pacotes winget, NAS, Claude Code, whisper, pausa por jogo) |

**Como medir velocidades:** rode a mesma tarefa real nos dois lados (`time` no Mac; o log do job no PC mostra a duração
em `pc jobs`) e grave a razão: se o PC fez em metade do tempo, `[2.0, 1.0]`. Quem tem `pref` "carga" divide por
velocidade × folga. Para mudar a preferência de um tipo, edite `TIPOS` em `pc.py`.

## Segurança (leia antes de mexer)

- O agente do PC executa **qualquer comando** de quem tiver o token e lê/escreve qualquer arquivo do usuário. O token
  fica em `config.json` (modo 600) dos dois lados. Nunca imprima, cole ou comite o token; `pc config` o esconde.
- A porta do agente só deve estar aberta para a rede local (e Tailscale, se usado), e o tráfego na rede local é HTTP
  sem criptografia: não use a Ponte em Wi-Fi que você não controla. Nunca abra a porta no roteador.
- O servidor do instalador (`pc instalar`) serve o token e a sua chave pública na rede local enquanto espera o PC:
  rode só quando for colar o comando no PC, e feche (Ctrl+C) se desistir.
- Mais em `docs/SEGURANCA.md` do repositório.
