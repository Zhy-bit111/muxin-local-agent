# 用统一口径重跑三版评测：基座 / v1 / v2
# 口径改过（提醒补了语义等价词、边界加了应承黑名单），旧的 base/lora 报告要作废重跑，
# 否则三版分数不可比。
$root = "E:\AI-Work\desk-companion"
$py = "$root\.venv\Scripts\python.exe"
$out = "$root\output"
Set-Location -LiteralPath $root

function Write-Log($m) {
    "[eval] $(Get-Date -Format 'HH:mm:ss') $m" | Out-File -LiteralPath "$out\eval_all.log" -Append -Encoding utf8BOM
}

if (Test-Path -LiteralPath "$out\eval_all.done") { Remove-Item -LiteralPath "$out\eval_all.done" -Force }
"" | Out-File -LiteralPath "$out\eval_all.log" -Encoding utf8BOM

Write-Log "开始 base（纯基座）"
& $py "scripts\eval.py" --no-adapter --tag base *> "$out\eval_base.log"
Write-Log "base 结束，退出码 $LASTEXITCODE"

Write-Log "开始 lora（v1）"
& $py "scripts\eval.py" --adapter "output/lora-qwen25-1.5b" --tag lora *> "$out\eval_lora.log"
Write-Log "lora 退出码 $LASTEXITCODE"

Write-Log "开始 v2（边界扩充版）"
& $py "scripts\eval.py" --adapter "output/lora-v2" --tag v2 *> "$out\eval_v2.log"
Write-Log "v2 退出码 $LASTEXITCODE"

Write-Log "全部完成"
"done" | Out-File -LiteralPath "$out\eval_all.done" -Encoding utf8BOM
