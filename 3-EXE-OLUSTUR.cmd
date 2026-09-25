@echo off
chcp 65001 > nul
title Tekeat - Exe olustur
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0betikler\derle.ps1"
echo.
echo Pencereyi kapatmak icin bir tusa basin.
pause > nul
