param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ToolArgs)
$ErrorActionPreference = 'Stop'
$appliance = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$image = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'image-id.txt') -Raw).Trim()
if ($image -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Invalid immutable image identity' }
$name = 'sorx-codex-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$argsList = @('run','--rm','-i','--network','none','--name',$name,'--label','sorx.owner=codex','-e','PYTHONDONTWRITEBYTECODE=1','-e',('SORX_IMAGE_ID=' + $image),
 '--mount', ('type=bind,source=' + (Join-Path $appliance 'hardware') + ',target=/work/hardware'),
 '--mount', ('type=bind,source=' + (Join-Path $appliance 'docs') + ',target=/work/docs'),
 '--mount', ('type=bind,source=' + (Join-Path $appliance 'baseline') + ',target=/work/baseline,readonly'),
 '--mount', ('type=bind,source=' + (Join-Path $appliance 'reference_images') + ',target=/work/reference_images,readonly'),
 '-w','/work/hardware/tools',$image)
& docker @argsList @ToolArgs
exit $LASTEXITCODE
