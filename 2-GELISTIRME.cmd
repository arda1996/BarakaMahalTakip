@echo off
chcp 65001 > nul
title Tekeat - Gelistirme
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0betikler\gelistir.ps1"
echo.
echo Pencereyi kapatmak icin bir tusa basin.
pause > nul
