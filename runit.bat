@echo off
start "Sample Splitter" py -3.11 samplesplitter.py
timeout /t 2 /nobreak >nul
start "" http://localhost:9876/
