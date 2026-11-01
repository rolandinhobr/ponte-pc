# ponte-pc

**Use o seu PC com GPU como o braço pesado do seu Mac, e deixe o Claude Code decidir onde cada tarefa roda.**

*English, short version: Ponte lets Claude Code (or you) offload renders, encodes, transcription and AI jobs from a Mac to a
Windows PC with an NVIDIA GPU, and decides per task which machine is faster and free. An installer asks about your
setup (GPU, NAS, Tailscale, CPU ceilings...) and configures everything. Mac → Windows only for now; docs are in
Portuguese. See [docs/SEGURANCA.md](docs/SEGURANCA.md) for the security model: the PC agent is an authenticated remote
shell, use it only on networks you control.*

---

## O que é

- Um agente (`windows/agente.py`) que roda no PC, mede CPU/GPU/VRAM e executa jobs que o Mac manda por HTTP.
- Um despachante (`pc`) no Mac: `pc onde encode --min 5` diz **MAC** ou **PC** (ou **ESPERAR**) olhando a carga das duas
  máquinas, a velocidade de cada uma para aquele tipo de tarefa e o custo de enviar os arquivos; `pc roda` manda; `pc log`,
  `pc espera`, `pc envia`, `pc traz`, `pc tela`...
- Uma **skill do Claude Code** (`skill/ponte-pc`) que ensina o Claude a consultar o `pc onde` antes de qualquer render,
  encode, transcrição ou geração por IA, e a **instalar tudo fazendo perguntas** sobre o seu computador.
- Opcionais: tradução de caminhos de NAS Mac ↔ Windows, acesso de fora de casa por Tailscale, pausa automática quando
  você abre um jogo, reserva da GPU para um projeto, transcrição com faster-whisper no PC (substituto direto do
  `mlx_whisper`).

A ideia vem do uso diário do autor (MacBook + PC com RTX 5080 + NAS) e foi generalizada para ser ajustada ao seu setup.

## Instalar

Você precisa de: um Mac, um PC com Windows 10/11 na mesma rede (ou no mesmo Tailscale), o Python 3 do Mac e, de
preferência, o [Claude Code](https://claude.com/claude-code).

### Com o Claude Code (recomendado)

```bash
git clone https://github.com/rolandinhobr/ponte-pc.git ~/Developer/ponte-pc
cd ~/Developer/ponte-pc
python3 install.py --avancado   # ou só "python3 install.py"
```

Ou peça ao Claude: *"instala a ponte-pc"* depois de copiar a pasta `skill/ponte-pc` para `~/.claude/skills/`. A
skill lê o `perguntas.json`, faz as perguntas com a ferramenta de perguntas do Claude Code, mostra o que vai fazer
(`--simular`) e só então instala.

### O que o instalador pergunta

| pergunta | para quê |
|---|---|
| computador principal e de trabalho | hoje só Mac → Windows (veja Roteiro) |
| placa de vídeo do PC | perfil `nvidia` (NVENC/NVDEC/CUDA) ou `sem-gpu` (PC é só mais CPU) |
| de casa ou também de fora (Tailscale) | instala o Tailscale no PC e libera o firewall dele |
| NAS (host, share, onde monta no Mac) | tradução de caminhos e roteamento quando o Mac está sem o NAS |
| módulos: transcrição, Claude Code no PC, pausa por jogo | o que mais entra no PC |
| programas base do winget (+ extras) | Python, FFmpeg, PowerShell 7, uv, Git, o que você quiser |
| teto de uso do Mac (com você usando / longe) | quanto o Mac aceita trabalhar em paralelo |
| avançado: faixa de rede, porta | só com `--avancado` |

As respostas viram `~/.config/ponte/config.json`; você pode editá-lo depois (veja a seção Ajustar). Quem prefere
automatizar usa `python3 install.py --respostas respostas.json` (`--perguntas` imprime a lista).

### Passo a passo

1. **No Mac:** `python3 install.py`. Cria a config, instala `pc` em `~/.local/bin`, a skill, e garante uma chave ssh.
   Adicione `~/.local/bin` ao PATH se ele pedir.
2. **No PC:** o instalador termina chamando `pc instalar`, que imprime algo como
   `irm http://192.168.1.10:8765/instalar.ps1 | iex`. Abra o **Terminal como administrador** no PC (botão direito no
   Iniciar), cole e espere. Se você marcou NAS, ele pede usuário e senha do NAS **uma vez** no PC. Leia o
   `ponte/windows/instalar.ps1` antes, se quiser: é curto e comentado.
3. **Confira:** `pc doctor` e `pc estado`. Rode algo pequeno: `pc roda --espera -- 'hostname'`.
4. **Calibre** (opcional, vale a pena): as velocidades padrão são do autor. Rode uma tarefa real nos dois lados e
   ajuste `velocidade` no config (exemplo abaixo).

## Usar

```bash
pc estado                                       # carga do Mac e do PC
pc onde encode --min 5 --entrada clipe.mov      # → "PC: PC com mais folga x velocidade ..."
pc roda --nome x --gpu --espera -- 'ffmpeg -hwaccel cuda -i entrada.mov -c:v hevc_nvenc saida.mp4'
pc onde encode --lote 12                        # → divide 12 itens entre PC e Mac, nas proporções certas
pc jobs ; pc log <id> -f ; pc cancela <id>
pc envia arquivo.mov ; pc traz 'C:\caminho\saida.mp4' .
pc tela                                         # screenshot do PC
pc pausa / pc retoma                            # tira o PC do processo
pc reserva-gpu --dono "MEU FILME" --horas 72    # GPU só para um projeto
pc atualiza                                     # manda um agente novo ao PC depois de atualizar o repositório
```

Com o Claude Code, a skill faz isso sozinha: *"renderiza esse vídeo em 4K"* vira `pc onde 4k --entrada ...` e depois
`pc roda`. Tipos de tarefa: `leve`, `encode`, `prores`, `422`, `4k`, `av1`, `transcricao`, `cuda`, `ia-imagem`,
`ia-video`, `llm`, `llm-grande`, `gui`, `render`, `cpu`, `build`.

### A política

As duas máquinas trabalham juntas e nenhuma fica parada com trabalho na fila. O Mac só aceita tarefa até `teto_mac`
% de CPU/GPU enquanto você usa (`teto_mac_ausente` quando você sai por 10 min); o PC aceita até `pc_cheio` %
(95), com 1,2 GB de VRAM livres. Pausado (por jogo ou `pc pausa`) ou fora do ar, o PC não entra na conta. Para cada tarefa
o `pc onde` compara `velocidade × folga` de cada lado, soma o custo de enviar os arquivos de entrada e responde.

## Ajustar ao seu setup

`~/.config/ponte/config.json` (`pc config` mostra; um exemplo em [examples/config.example.json](examples/config.example.json)):

```jsonc
{
  "perfil": "nvidia",                  // ou "sem-gpu"
  "velocidade": {"encode": [6.0, 7.0]}, // [PC, Mac] por tipo; vale por cima da tabela do autor
  "teto_mac": 60,                      // % que o Mac aceita com você usando
  "nas_mac": "/Volumes/Projetos", "nas_host": "192.168.1.50", "nas_share": "Projetos",
  "tailscale": true
}
```

- **Medir velocidades:** rode a mesma tarefa real nos dois lados; se o PC levou metade do tempo, `[2.0, 1.0]`.
- **Mudar a preferência de um tipo** (ex.: `render` sempre no PC): edite `TIPOS` em `ponte/pc.py` (`"pc"`, `"mac"`,
  `"carga"`, `"pc!"`). `perfil_tipos()` é onde o perfil `sem-gpu` reescreve a tabela.
- **Mais programas no PC:** `instalar.pacotes` (IDs do `winget`), ou `pc roda -- 'winget install ...'` depois.
- **Outra GPU/encoder:** as receitas do FFmpeg estão na skill; troque `hevc_nvenc` pelo que a sua placa tiver.

## O que o instalador mexe

- **Mac:** `~/.config/ponte/` (config e token), `~/.local/share/ponte/` (código, jobs, medidas), `~/.local/bin/pc` (link),
  `~/.claude/skills/ponte-pc/` (a antiga, se existir, é guardada ao lado), `~/.ssh/id_ed25519` (só se não existir) e, depois do
  `pc instalar`, um bloco `# >>> ponte` no começo de `~/.ssh/config` com o host `pc`.
- **PC:** servidor SSH só com chave, regras de firewall da Ponte, sem suspensão na tomada, caminhos longos, a tarefa
  agendada "Ponte Agente" (inicia no logon), a pasta `%LOCALAPPDATA%\Ponte`, e os programas que você marcou no winget.

## Segurança, em uma frase

O agente do PC é um shell remoto protegido por token: use só em rede sua, nunca abra a porta no roteador, e leia
[docs/SEGURANCA.md](docs/SEGURANCA.md). Remover tudo: `ponte/windows/desinstalar.ps1` no PC e
`python3 install.py --desinstalar` no Mac.

## Estrutura

```
install.py               instalador interativo do Mac (lê perguntas.json)
perguntas.json           as perguntas (o install.py e a skill usam a mesma lista)
skill/ponte-pc/          skill do Claude Code
ponte/pc.py              despachante do Mac (python puro, sem dependências)
ponte/ponte_whisper.py   substituto do mlx_whisper que usa o PC quando livre
ponte/windows/           agente.py, instalar.ps1, desinstalar.ps1, whisper_ponte.py (vão para o PC)
docs/SEGURANCA.md
```

Dependências: só a biblioteca padrão do Python no Mac. No PC, o instalador pega `psutil`, `pystray` e `pillow` por pip.

## Estado e testes

- **Testado antes da publicação:** o despachante e o agente conversando (macOS, agente local), `onde`/`roda`/`jobs`/`log`/
  `envia`/`traz`/`pausa`, autenticação por token (401 sem token), o `install.py` em um HOME isolado, a geração do
  `instalar.ps1` a partir da config e a análise de sintaxe dele (sem executá-lo) no PowerShell de um Windows.
- **Não testado em máquina limpa:** o `instalar.ps1` inteiro executado do zero, os módulos de Tailscale e transcrição
  com a configuração genérica (existem no setup do autor, com caminhos próprios). Se algo quebrar, abra uma issue com o
  `instalacao.log` (em `%LOCALAPPDATA%\Ponte` no PC).

## Roteiro (ainda não incluído)

O setup do autor tem mais módulos que dependem de apps ou serviços específicos e ainda não foram generalizados: espelho
de projetos locais no PC, render de projetos do DaVinci Resolve no PC, delegação de tarefas inteiras a um agente do Claude
Code / Codex no PC, e "modos" que a skill liga e desliga. Também falta: controlador em Windows/Linux e máquina de
trabalho Linux/Mac. Contribuições bem-vindas.

## Licença

MIT.
