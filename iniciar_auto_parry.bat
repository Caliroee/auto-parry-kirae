@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Auto-Parry Kirae v1.1.1 - RCM/Caliroe

echo ==============================================
echo    AUTO-PARRY KIRAE  v1.1.1   -   por RCM/Caliroe
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
echo Python encontrado: %PYTHON%
%PYTHON% -c "import mss, numpy, PIL, pynput" >nul 2>nul
if errorlevel 1 (
    echo Instalando dependencias ^(somente na primeira vez^)...
    %PYTHON% -m pip install --upgrade mss numpy pillow pynput
    if errorlevel 1 goto erro
)

echo.
echo Iniciando Auto-Parry Kirae...
%PYTHON% "%~dp0auto_parry_kirae.py"
if errorlevel 1 goto erro
exit /b 0

:erro
echo.
echo Ocorreu um erro durante a instalacao ou execucao.
echo Verifique as mensagens acima e tente novamente.
pause
exit /b 1
