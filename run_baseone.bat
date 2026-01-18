@echo off
cd %USERPROFILE%\Desktop\BaseOne
start cmd /k python server.py
timeout /t 3
start http://127.0.0.1:5000
