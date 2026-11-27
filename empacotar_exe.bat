@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Empacotar Auto-Parry Kirae v1.1.1 - RCM/Caliroe

echo ==============================================
echo    EMPACOTAR AUTO-PARRY KIRAE v1.1.1 (.exe)
echo    por RCM/Caliroe
echo ==============================================
echo.

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py -3"
    goto python_ok
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=python"
    goto python_ok
)

echo Python nao foi encontrado neste computador.
echo Abrindo a pagina oficial para instalacao...
start "" "https://www.python.org/downloads/windows/"
echo.
echo IMPORTANTE: durante a instalacao, marque a opcao "Add Python to PATH".
echo Depois de instalar, feche esta janela e execute este arquivo novamente.
echo.
pause
exit /b 1

:python_ok
%PYTHON% -m pip install --upgrade pyinstaller mss numpy pillow pynput
if errorlevel 1 goto erro

%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name AutoParryKirae --icon "assets\rcm_icon.ico" ^
    --add-data "assets;assets" --collect-submodules pynput ^
    auto_parry_kirae.py
if errorlevel 1 goto erro

echo.
echo Pronto: dist\AutoParryKirae.exe
echo Em caso de falha ao abrir, veja auto_parry_erro.log ao lado do .exe.
pause
exit /b 0

:erro
echo.
echo Falha ao empacotar. Verifique as mensagens acima.
pause
exit /b 1
