# Ponte: instalação no PC. Roda UMA vez, no Terminal aberto como administrador.
# O Mac serve este arquivo já preenchido (pc instalar); os marcadores __X__ viram valores a partir das respostas do
# install.py (config.json -> "instalar"). Leia antes de rodar: ele abre SSH só para a sua chave, libera a porta do agente
# só para a rede local (e Tailscale, se você usa) e instala os programas listados em $Pacotes.

# Continue, e não Stop: no PowerShell 5, stderr de winget/nvidia-smi redirecionado vira erro fatal com Stop.
# Cada passo é idempotente; rodar de novo conserta o que falhar.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Base     = '__BASE__'
$Token    = '__TOKEN__'
$ChaveMac = '__CHAVE__'
$NasHost  = '__NASHOST__'
$NasShare = '__NASSHARE__'
$Porta    = __PORTA__
$Pacotes  = @(__PACOTES__)       # pacotes do winget
$UsarNas  = __USARNAS__          # liga o acesso ao NAS por UNC
$InstalarClaude = __CLAUDECODE__ # Claude Code no PC
$Whisper  = __WHISPER__          # servidor de transcrição (faster-whisper em CUDA)
$Tailscale = __TAILSCALE__       # libera também a rede do Tailscale (100.64.0.0/10) no firewall
$PausaJogos = __PAUSAJOGOS__   # o agente se pausa sozinho quando um jogo abre em primeiro plano
$Remoto = if ($Tailscale) { 'LocalSubnet', '100.64.0.0/10' } else { 'LocalSubnet' }

function Passo($texto) { Write-Host "`n==> $texto" -ForegroundColor Cyan }
function Aviso($texto) { Write-Host "    $texto" -ForegroundColor Yellow }
function AtualizarPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

$eu = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $eu.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Abra o Terminal como administrador (botão direito no Iniciar > Terminal (Admin)) e cole de novo.' -ForegroundColor Red
    return
}

$Dir = Join-Path $env:LOCALAPPDATA 'Ponte'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Start-Transcript -Path (Join-Path $Dir 'instalacao.log') -Force | Out-Null

if ($UsarNas) {
    # só com NAS: o Windows costuma recusar SMB em rede "pública". Marca TODAS as redes públicas como privadas;
    # num desktop de casa tudo bem, num notebook que anda por aí prefira marcar só a sua rede à mão e apagar este passo.
    Passo 'Rede de casa como privada (para o NAS)'
    Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } | Set-NetConnectionProfile -NetworkCategory Private
}

Passo 'Caminhos longos no Windows'
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force | Out-Null

Passo 'Nunca dormir na tomada'
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

Passo 'Servidor SSH (só chave do Mac, só rede local)'
$cap = Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' } | Select-Object -First 1
if ($cap -and $cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd

$chaves = @(
    (Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'),
    (Join-Path $env:USERPROFILE '.ssh\authorized_keys')
)
foreach ($arq in $chaves) {
    New-Item -ItemType Directory -Force -Path (Split-Path $arq) | Out-Null
    $atuais = @()
    if (Test-Path $arq) { $atuais = @(Get-Content $arq) }
    if ($atuais -notcontains $ChaveMac) { Add-Content -Path $arq -Value $ChaveMac -Encoding ascii }
}
# SIDs em vez de nomes: no Windows em português o grupo se chama "Administradores"
icacls.exe $chaves[0] /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null

$cfgSsh = Join-Path $env:ProgramData 'ssh\sshd_config'
$texto = Get-Content $cfgSsh -Raw
if ($texto -match '(?m)^#?\s*PasswordAuthentication\s+\S+') {
    $texto = $texto -replace '(?m)^#?\s*PasswordAuthentication\s+\S+', 'PasswordAuthentication no'
} else {
    $texto = "PasswordAuthentication no`r`n" + $texto
}
$texto = $texto -replace '(?m)^#?\s*PubkeyAuthentication\s+\S+', 'PubkeyAuthentication yes'
Set-Content -Path $cfgSsh -Value $texto -Encoding ascii -NoNewline

$regra = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($regra) {
    $regra | Set-NetFirewallRule -Profile Any -Enabled True
    $regra | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $Remoto
} else {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Direction Inbound `
        -Protocol TCP -LocalPort 22 -Action Allow -RemoteAddress $Remoto -Profile Any | Out-Null
}

Passo 'Programas (winget)'
foreach ($p in $Pacotes) {
    winget list -e --id $p --accept-source-agreements *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "    $p já instalado"; continue }
    Write-Host "    instalando $p"
    winget install -e --id $p --silent --accept-package-agreements --accept-source-agreements --disable-interactivity *> $null
    if ($LASTEXITCODE -ne 0) { Aviso "winget devolveu $LASTEXITCODE para $p (sigo; o Mac confere depois)" }
}
AtualizarPath

$pwsh = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
$shellSsh = if (Test-Path $pwsh) { $pwsh } else { Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe' }
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value $shellSsh -PropertyType String -Force | Out-Null
Restart-Service sshd

$py = $null
foreach ($c in @((Join-Path $env:ProgramFiles 'Python312\python.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))) {
    if (Test-Path $c) { $py = $c; break }
}
if (-not $py) {
    $py = (& py -3.12 -c 'import sys; print(sys.executable)' 2>$null)
}
if (-not $py -or -not (Test-Path $py)) { throw 'Python 3.12 não apareceu depois do winget. Rode de novo; se repetir, me mande o instalacao.log.' }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
& $py -m pip install --upgrade --quiet --disable-pip-version-check psutil pystray pillow
if ($LASTEXITCODE -ne 0) { Aviso 'pip falhou; o agente roda com telemetria parcial' }

if ($InstalarClaude) {
    Passo 'Claude Code no PC'
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
        try { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression; AtualizarPath }
        catch { Aviso "instalação do Claude Code falhou: $($_.Exception.Message)" }
    }
}

$unc = ''
if ($UsarNas) {
    Passo 'Acesso ao NAS'
    $unc = "\\$NasHost\$NasShare"
    if (-not (Test-Path -LiteralPath $unc)) {
        $cred = Get-Credential -Message "Usuário e senha do NAS ($NasHost). Fica guardado no Gerenciador de Credenciais do Windows."
        if ($cred) {
            cmdkey /add:$NasHost /user:$($cred.UserName) /pass:$($cred.GetNetworkCredential().Password) | Out-Null
        }
    }
    if (Test-Path -LiteralPath $unc) { Write-Host "    NAS ok: $unc" } else { Aviso "não consegui abrir $unc; o agente funciona, mas sem o NAS" }
}

if ($Whisper) {
    Passo 'Transcrição (faster-whisper em CUDA)'
    $venvW = Join-Path $Dir 'venvs\whisper'
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { Aviso 'uv não encontrado; abra um terminal novo e rode o instalador de novo'; }
    else {
        if (-not (Test-Path (Join-Path $venvW 'Scripts\python.exe'))) { & $uv venv --python 3.12 $venvW | Out-Null }
        & $uv pip install --quiet --python (Join-Path $venvW 'Scripts\python.exe') faster-whisper numpy nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'
        if ($LASTEXITCODE -ne 0) { Aviso 'a instalação do faster-whisper falhou; a transcrição continua no Mac' }
        Invoke-WebRequest -Uri "$Base/whisper_ponte.py" -OutFile (Join-Path $Dir 'whisper_ponte.py') -UseBasicParsing
    }
}

Passo 'Agente da Ponte'
Get-ScheduledTask -TaskName 'Ponte Agente' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Invoke-WebRequest -Uri "$Base/agente.py" -OutFile (Join-Path $Dir 'agente.py') -UseBasicParsing
$cfg = [ordered]@{ porta = $Porta; token = $Token; nas_unc = $unc; auto_pausa_jogos = [bool]$PausaJogos }
$cfgArq = Join-Path $Dir 'config.json'
if (Test-Path $cfgArq) {
    $velho = Get-Content $cfgArq -Raw | ConvertFrom-Json
    foreach ($prop in $velho.PSObject.Properties) { if (-not $cfg.Contains($prop.Name)) { $cfg[$prop.Name] = $prop.Value } }
}
$cfg | ConvertTo-Json -Depth 5 | Set-Content -Path $cfgArq -Encoding UTF8

foreach ($nome in 'Ponte-Agente', 'Ponte-Agente-Programa') { Remove-NetFirewallRule -Name $nome -ErrorAction SilentlyContinue }
New-NetFirewallRule -Name 'Ponte-Agente' -DisplayName 'Ponte (agente)' -Direction Inbound -Protocol TCP `
    -LocalPort $Porta -Action Allow -RemoteAddress $Remoto -Profile Any | Out-Null
# regra por programa evita o aviso "o Firewall bloqueou alguns recursos" na primeira escuta
New-NetFirewallRule -Name 'Ponte-Agente-Programa' -DisplayName 'Ponte (pythonw)' -Direction Inbound -Protocol TCP `
    -LocalPort $Porta -Program $pyw -Action Allow -RemoteAddress $Remoto -Profile Any | Out-Null

$usuario = "$env:USERDOMAIN\$env:USERNAME"
$acao = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Dir\agente.py`"" -WorkingDirectory $Dir
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User $usuario
$quem = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Highest
$ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Ponte Agente' -Action $acao -Trigger $gatilho -Principal $quem -Settings $ajustes -Force | Out-Null
Start-ScheduledTask -TaskName 'Ponte Agente'
Start-Sleep -Seconds 4
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:$Porta/saude" -TimeoutSec 5 | Out-Null
    Write-Host '    agente no ar (ícone na bandeja, perto do relógio)'
} catch { Aviso "o agente não respondeu ainda; veja $Dir\agente.log" }

Passo 'Avisando o Mac'
$rede = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1
$ip = @($rede.IPv4Address)[0].IPAddress
$gpu = ''
try { $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1) } catch {}
$tsIp = ''
try { $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1) } catch {}
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
$ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$info = [ordered]@{
    hostname = $env:COMPUTERNAME; ip = $ip; usuario = $env:USERNAME; gpu = "$gpu".Trim(); cpu = "$cpu".Trim()
    ram_gb = $ram; dir = $Dir; python = $py; claude = [bool](Get-Command claude -ErrorAction SilentlyContinue)
    nas_ok = ($unc -and (Test-Path -LiteralPath $unc)); adaptador = $rede.InterfaceAlias; tailscale_ip = "$tsIp".Trim()
}
$corpo = [Text.Encoding]::UTF8.GetBytes(($info | ConvertTo-Json))
try {
    Invoke-RestMethod -Method Post -Uri "$Base/pronto" -Body $corpo -ContentType 'application/json; charset=utf-8' | Out-Null
    Write-Host '    Mac avisado.'
} catch { Aviso "não consegui avisar o Mac ($($_.Exception.Message)). IP deste PC: $ip" }

Stop-Transcript | Out-Null
Write-Host "`nPronto. Pode fechar esta janela." -ForegroundColor Green
if ($InstalarClaude) {
    if (-not $info.claude) { Aviso 'Claude Code não instalou; rode o instalador de novo.' }
    else { Write-Host 'Último passo manual: abra um Terminal normal, digite  claude  e faça o login uma vez.' -ForegroundColor Green }
}
