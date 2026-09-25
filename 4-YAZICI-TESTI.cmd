@echo off
chcp 65001 > nul
title Tekeat - Yazici testi
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0betikler\yazici.ps1" %*
echo.
echo Pencereyi kapatmak icin bir tusa basin.
pause > nul
