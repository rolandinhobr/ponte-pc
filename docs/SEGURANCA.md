# Segurança da Ponte

Leia antes de instalar. A Ponte é, de propósito, **um shell remoto autenticado por token** para o seu PC.

## O que o agente permite a quem tem o token

- Executar qualquer comando no PC, com as permissões do usuário do Windows que o iniciou (o instalador o registra com
  privilégio elevado: `RunLevel Highest`, para poder instalar programas por `pc roda`).
- Ler e gravar qualquer arquivo que esse usuário alcance (`/arquivos`), e capturar a tela (`/tela`).

Quem não tem o token recebe 401 em tudo, menos em `/saude`, que só responde "estou aqui" e prova que conhece o token
(HMAC de um número que o Mac manda), sem revelá-lo. O agente **não sobe sem token**.

## O que protege

- **Token** aleatório de 24 bytes, gerado no primeiro `pc instalar`, guardado em `~/.config/ponte/config.json` no Mac
  (modo 600) e em `%LOCALAPPDATA%\Ponte\config.json` no PC.
- **Firewall do Windows**: a porta do agente (17850 por padrão) e a do SSH só aceitam a sub-rede local
  (`LocalSubnet`) e, se você escolheu Tailscale, `100.64.0.0/10`. Nunca abra a porta no roteador.
- **SSH** só por chave: o instalador adiciona a chave pública do Mac e desliga a senha do `sshd`.

## O que NÃO protege

- O tráfego do agente na rede local é **HTTP sem criptografia**. Quem estiver na sua rede e conseguir observá-la vê
  comandos, saídas e o token. Use em rede que você controla (casa, escritório). Fora de casa, o Tailscale cifra o caminho.
- O **servidor do instalador** (`pc instalar`) entrega o `.ps1` com o token embutido a qualquer aparelho da rede local que o
  peça enquanto está de pé (padrão 15 min). Rode só diante do PC e feche com Ctrl+C se desistir.
- Quem controla o Mac controla o PC (o token está no Mac), e vice-versa.

## Boas práticas

- Rode a Ponte só em PC e rede seus. Não instale em máquina de trabalho de terceiros.
- Para trocar o token: apague `"token"` de `~/.config/ponte/config.json` e rode `pc instalar` de novo.
- Para remover: `ponte/windows/desinstalar.ps1` no PC (Terminal como administrador) e `python3 install.py --desinstalar`
  no Mac. O `desinstalar.ps1` só remove da lista de chaves do PC as linhas terminadas em ` ponte` (o comentário que o
  `install.py` dá à chave que ele cria); se você reaproveitou uma chave sua, apague a linha à mão
  (`C:\ProgramData\ssh\administrators_authorized_keys` e `%USERPROFILE%\.ssh\authorized_keys`).
- O instalador do PC altera: horário de suspensão na tomada (nunca dormir), caminhos longos, servidor SSH, regras de firewall,
  e, **se você usa NAS**, marca redes "públicas" como "privadas" (necessário para SMB em muitos casos). Leia
  `ponte/windows/instalar.ps1` antes de rodar; é curto.
- Reporte vulnerabilidades por issue privada (GitHub Security Advisories).
