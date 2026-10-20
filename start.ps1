# Windows PowerShell 5.1 / PowerShell 7；菜单不会自动安装或启动播放。
$ErrorActionPreference = 'Stop'

function Test-HelperPython {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        Write-Host '未找到 py 启动器。请先安装 Python 3.10+（含启动器和 pip）。'
        return $false
    }
    & py -3 -X utf8 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) {
        Write-Host '需要可用的 Python 3.10 或更新版本。'
        return $false
    }
    return $true
}

function Test-HelperDependencies {
    if (-not (Test-HelperPython)) {
        return
    }
    & py -3 -X utf8 --version
    & py -3 -X utf8 -c "import importlib.util; import sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '尚未找到 Playwright。请自行执行：py -m pip install -r requirements.txt'
        return
    }
    & py -3 -X utf8 -m pip show playwright
    Write-Host '以上仅检查本地依赖，未打开浏览器。请确认 Edge 已安装，再选择 inspect。'
}

function Invoke-Helper {
    param([string[]]$HelperArguments)
    if (-not (Test-HelperPython)) {
        return
    }
    & py -3 -X utf8 (Join-Path $PSScriptRoot 'main.py') @HelperArguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "主程序已退出，退出码：$LASTEXITCODE。请查看上方信息或 README.md。"
    }
}

Push-Location -LiteralPath $PSScriptRoot
try {
    $helperMenuOpen = $true
    while ($helperMenuOpen) {
        Write-Host ''
        Write-Host '网课播放辅助'
        Write-Host '1. 检查依赖（不安装）'
        Write-Host '2. inspect 超星（先登录并打开具体章节）'
        Write-Host '3. run 超星（确认页面后尝试播放）'
        Write-Host '4. status（查看本地记录）'
        Write-Host '0. 退出'
        $helperChoice = Read-Host '请选择'
        switch ($helperChoice) {
            '1' { Test-HelperDependencies }
            '2' { Invoke-Helper -HelperArguments @('inspect', '--platform', 'chaoxing') }
            '3' {
                Write-Host '请先完成 inspect。每门课程请使用不同且固定的任务 ID。'
                $helperTaskId = (Read-Host '任务 ID（例如 my-course；留空返回菜单）').Trim()
                if ($helperTaskId) {
                    $helperArguments = @('run', '--platform', 'chaoxing', '--task-id', $helperTaskId)
                    $helperResume = Read-Host '是否恢复上次章节？输入 y 恢复；其他输入从平台首页开始'
                    if ($helperResume.Trim().ToLowerInvariant() -eq 'y') {
                        $helperArguments += '--resume'
                    }
                    Invoke-Helper -HelperArguments $helperArguments
                }
            }
            '4' { Invoke-Helper -HelperArguments @('status') }
            '0' { $helperMenuOpen = $false }
            default { Write-Host '请输入 1、2、3、4 或 0。' }
        }
    }
}
finally {
    Pop-Location
}
