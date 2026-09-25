@echo off
chcp 65001 > nul
title Tekeat - Kurulum
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0betikler\kurulum.ps1"
echo.
echo Pencereyi kapatmak icin bir tusa basin.
pause > nul
