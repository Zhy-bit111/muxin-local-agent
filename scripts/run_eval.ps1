# 串行跑两轮评测：基座（--no-adapter --tag base）与 LoRA（--tag lora）
# 串行是为了避免两个进程同时抢 8GB 显存；日志分开留档，互不覆盖。
$ErrorActionPreference = 'Continue'
$root = "E:\AI-Work\desk-companion"
$py = "$root\.venv\Scripts\python.exe"
$out = "$root\output"

Set-Location -LiteralPath $root

function Write-RunLog($msg) {
    $line = "[eval] $(Get-Date -Format 'HH:mm:ss') $msg"
    $line | Out-File -LiteralPath "$out\eval_run.log" -Append -Encoding utf8BOM
}

Write-RunLog "开始 base 评测（基座，不挂 adapter）"
& $py "scripts\eval.py" --no-adapter --tag base *> "$out\eval_base.log"
Write-RunLog "base 结束，退出码 $LASTEXITCODE"

Write-RunLog "开始 lora 评测（挂 output/lora-qwen25-1.5b）"
& $py "scripts\eval.py" --tag lora *> "$out\eval_lora.log"
Write-RunLog "lora 结束，退出码 $LASTEXITCODE"

Write-RunLog "全部完成"
"done" | Out-File -LiteralPath "$out\eval_run.done" -Encoding utf8BOM
