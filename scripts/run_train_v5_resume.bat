@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" "scripts\train_lora.py" --config "configs\lora_qwen25_1.5b_v5.yaml" --resume > "output\train_v5b.log" 2> "output\train_v5b.err.log"
