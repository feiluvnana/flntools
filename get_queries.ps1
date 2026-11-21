# Twitter/X Batch Runner for flnd
# Automatically downloads both profile media (twt:<user>) and search queries (twt?<query>)

$ErrorActionPreference = "Continue"

# Prevent Windows from sleeping or suspending background tasks while running
if (-not ('WinPower' -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class WinPower {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern int SetThreadExecutionState(int esFlags);
}
"@
}
# ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001) | ES_AWAYMODE_REQUIRED (0x00000040)
[WinPower]::SetThreadExecutionState([int]0x80000041) | Out-Null

try {
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host "  flnd Twitter/X Profile & Query Batch Runner" -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host ""

$tasks = @(
    # 1. Pyo Eunji (Pyoapple)
    @{
        Number = 1
        Name   = "Pyo Eunji - Personal Profile"
        Target = "twt:pyoapple"
        Folder = ""
    },
    @{
        Number = 2
        Name   = "Pyo Eunji - Japan Profile"
        Target = "twt:PyoEunjijapan"
        Folder = ""
    },
    # 2. Marina Nagasawa
    @{
        Number = 3
        Name   = "Marina Nagasawa - Profile"
        Target = "twt:nagasawa_marina"
        Folder = ""
    },
    @{
        Number = 4
        Name   = "Marina Nagasawa - Query"
        Target = 'twt?("長澤茉里奈" OR @nagasawa_marina OR "Marina Nagasawa" OR "Nagasawa Marina" OR #長澤茉里奈 OR "長澤まりな") -from:nagasawa_marina'
        Folder = "nagasawa_marina-query"
    },
    # 3. Utatane Sui
    @{
        Number = 5
        Name   = "Utatane Sui - Profile"
        Target = "twt:meidomusumemoe"
        Folder = ""
    },
    @{
        Number = 6
        Name   = "Utatane Sui - Query"
        Target = 'twt?(meidomusumemoe OR @meidomusumemoe OR "meido musume moe" OR "うたたね翠" OR "うたたね 翠" OR "Utatane Sui" OR "Sui Utatane" OR #meidomusumemoe OR #うたたね翠) -from:meidomusumemoe'
        Folder = "meidomusumemoe-query"
    },
    # 4. Iri
    @{
        Number = 7
        Name   = "Iri - Profile"
        Target = "twt:irisuare"
        Folder = ""
    },
    @{
        Number = 8
        Name   = "Iri - Query"
        Target = 'twt?(irisuare OR @irisuare OR "愛莉 irisuare" OR "irisuare 愛莉" OR #irisuare) -from:irisuare'
        Folder = "irisuare-query"
    },
    # 5. Hiina Nano
    @{
        Number = 9
        Name   = "Hiina Nano - Profile"
        Target = "twt:nano_cats0923"
        Folder = ""
    },
    @{
        Number = 10
        Name   = "Hiina Nano - Query"
        Target = 'twt?(nano_cats0923 OR @nano_cats0923 OR "雛なの" OR "ひいななの" OR "Hiina Nano" OR "Nano Hiina" OR "すてねこキャッツ" OR #雛なの OR #すてねこキャッツ OR #nano_cats0923) -from:nano_cats0923'
        Folder = "nano_cats0923-query"
    },
    # 6. Chunmomo
    @{
        Number = 11
        Name   = "Chunmomo - Profile"
        Target = "twt:chunmomo0127"
        Folder = ""
    },
    @{
        Number = 12
        Name   = "Chunmomo - Query"
        Target = 'twt?(chunmomo0127 OR @chunmomo0127 OR "蠢沫沫" OR chunmomo OR "chun momo" OR "Chunmomo cos" OR #蠢沫沫 OR #chunmomo OR #chunmomo0127) -from:chunmomo0127'
        Folder = "chunmomo0127-query"
    },
    # 7. Nyaromenia
    @{
        Number = 13
        Name   = "Nyaromenia - Main Profile"
        Target = "twt:nyarome_cos"
        Folder = ""
    },
    @{
        Number = 14
        Name   = "Nyaromenia - Personal Profile"
        Target = "twt:nyaromenia"
        Folder = ""
    },
    @{
        Number = 15
        Name   = "Nyaromenia - Query"
        Target = 'twt?("ニャロメニア" OR @nyarome_cos OR @nyaromenia OR nyarome_cos OR nyaromenia OR "Nyarome Nia" OR "Nyaromenia" OR "にゃろめにあ" OR #ニャロメニア OR #nyarome_cos OR #nyaromenia) -from:nyarome_cos -from:nyaromenia'
        Folder = "nyaromenia-query"
    }
)

$total = $tasks.Count

foreach ($t in $tasks) {
    Write-Host "[$($t.Number)/$total] Downloading: $($t.Name)" -ForegroundColor Yellow
    if ($t.Folder) {
        Write-Host "Folder: $($t.Folder)" -ForegroundColor DarkGray
        $escapedTarget = $t.Target.Replace('"', '\"')
        & flnd get $escapedTarget --folder=$($t.Folder)
    } else {
        Write-Host "Target: $($t.Target)" -ForegroundColor DarkGray
        & flnd get $($t.Target)
    }
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: Step $($t.Number) finished with exit code $LASTEXITCODE" `
            -ForegroundColor Magenta
    } else {
        Write-Host "Completed: $($t.Name)" -ForegroundColor Green
    }
    Write-Host ""
}

    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host "  All profile and query downloads finished!" -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan
} finally {
    # Restore normal execution state: ES_CONTINUOUS (0x80000000)
    [WinPower]::SetThreadExecutionState([int]0x80000000) | Out-Null
}
