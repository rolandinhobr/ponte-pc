# Ponte: tira o agente deste PC. Rode no Terminal como administrador.
# Remove: a tarefa agendada, as regras de firewall da Ponte, a pasta %LOCALAPPDATA%\Ponte e a chave do Mac
# (a linha com o comentário "ponte") dos arquivos authorized_keys. Não desinstala Python, FFmpeg etc.
# Não desliga o servidor SSH: se você não o usava antes da Ponte, rode  Stop-Service sshd; Set-Service sshd -StartupType Disabled
$ErrorActionPreference = 'Continue'

Get-ScheduledTask -TaskName 'Ponte Agente' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'Ponte Agente' -Confirm:$false -ErrorAction SilentlyContinue
Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -and (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -like '*Ponte*agente.py*' } | Stop-Process -Force

foreach ($nome in 'Ponte-Agente', 'Ponte-Agente-Programa') { Remove-NetFirewallRule -Name $nome -ErrorAction SilentlyContinue }

foreach ($arq in (Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'), (Join-Path $env:USERPROFILE '.ssh\authorized_keys')) {
    if (Test-Path $arq) {
        $linhas = Get-Content $arq | Where-Object { $_ -notmatch '\sponte\s*$' }
        Set-Content -Path $arq -Value $linhas -Encoding ascii
    }
}

$dir = Join-Path $env:LOCALAPPDATA 'Ponte'
if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
Write-Host 'Ponte removida deste PC.' -ForegroundColor Green
