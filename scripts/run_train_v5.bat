@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" "scripts\train_lora.py" --config "configs\lora_qwen25_1.5b_v5.yaml" > "output\train_v5.log" 2> "output\train_v5.err.log"
