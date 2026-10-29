# Maintaining and publishing the MockX wiki

The canonical Markdown is in `doc/wiki/`. Its 15 content pages plus `_Sidebar.md` and `_Footer.md` are published unchanged to the separate [GitHub wiki](https://github.com/lehajamb/mockx/wiki) Git repository. This document stays in the source repository; it is not a wiki page.

The initial wiki documents published source commit `556e28b533171ceee63864e396faedfb2ef7aa8e`. Repository source links are pinned to that commit. The initial documentation change is delivered on `codex/wiki-docs`; use that branch until its documentation PR is merged.

## Update the source

1. Work in a clean checkout or isolated worktree of the intended published source revision. Do not derive public documentation from unrelated uncommitted work.
2. Edit the relevant files under `doc/wiki`. Keep the existing filenames stable because they determine wiki URLs. Update Home, sidebar, and cross-links when adding or renaming a page.
3. Check API/configuration statements and examples against implementation and tests. When documenting a newer revision, update source links, Home's revision statement, and the footer's revision and review date together.
4. Use absolute wiki URLs for cross-page links; they work in both GitHub's repository viewer and the wiki. Link repository files to their documented commit. Keep interactive HTML/JavaScript guides on GitHub Pages; use Markdown and Mermaid for wiki explanations.
5. Submit a documentation-only PR. Do not silently alter FIX, REST, WebSocket, configuration, or application behavior to fit the prose.

The current combined `doc/mockx-engine-scheduler.html` is explicitly historical. Prefer the separate architecture, engine, and scheduler guides as supporting sources.

## Validate before publishing

Check all page destinations and heading anchors; every sidebar entry must resolve. Confirm fenced blocks close, Mermaid diagrams render, and source links point to files that exist at the pinned revision. Cross-check every copied CLI flag, port, environment variable, payload, and configuration key.

In the repository's existing Windows Conda setup:

```powershell
. "C:\Users\lehaj\anaconda3\shell\condabin\conda-hook.ps1"
conda activate mockx
python -m pytest -q tests/test_readme_examples.py tests/test_time_control_api.py tests/test_api_lifecycle.py tests/test_websocket_market_data.py tests/test_pr9_operational_contracts.py
python -m pytest -q
```

Those environment paths describe the maintainer's workstation. Other contributors should use the installation instructions in the wiki. Report skips and missing native prerequisites explicitly; never describe a failed suite as passing.

## First publication setup

Wikis must be enabled under repository Settings → General → Features. MockX already has them enabled.

GitHub requires an initial page before the wiki can be cloned. If `mockx.wiki.git` returns “Repository not found” while wikis are enabled, sign into an account with write access, open the [Wiki tab](https://github.com/lehajamb/mockx/wiki), choose **Create the first page**, and save a **Home** page. Publication replaces that placeholder with the prepared Home page.

Use existing Git credentials for an account with repository write access. The GitHub application connector's permissions and the local GitHub CLI credentials are separate. Check the CLI without printing its token:

```powershell
gh auth status
gh api repos/lehajamb/mockx --jq '.permissions'
```

If authentication is missing, sign in interactively with `gh auth login --hostname github.com --git-protocol https --web`. Do not paste access tokens into documentation, chat, or commit history. If the account lacks write access, the repository owner must grant it or use an authorized account.

## Publish the prepared pages

Run this from the reviewed source checkout in PowerShell. It uses the existing GitHub CLI credential helper for each Git network command and does not change global Git credential settings.

```powershell
$wikiSource = (Resolve-Path -LiteralPath 'doc/wiki').Path
$wikiCheckout = Join-Path $env:TEMP ("mockx-wiki-" + [guid]::NewGuid().ToString('N'))
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' clone https://github.com/lehajamb/mockx.wiki.git $wikiCheckout
if ($LASTEXITCODE -ne 0) { throw 'Wiki clone failed; check initialization and access.' }

# Preserve all existing wiki pages that are not owned by doc/wiki.
Get-ChildItem -LiteralPath $wikiSource -Filter '*.md' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $wikiCheckout $_.Name)
}
git -C $wikiCheckout diff --check
git -C $wikiCheckout diff --stat
git -C $wikiCheckout diff
```

Review the complete diff. If an existing page with the same name has independent changes, reconcile those changes in `doc/wiki` before copying again. Deleting a file from `doc/wiki` does not automatically delete its published page; review intentional wiki removals separately.

Then stage only the prepared filenames, commit, and push the checked-out wiki default branch:

```powershell
$wikiFiles = @(Get-ChildItem -LiteralPath $wikiSource -Filter '*.md' -File | Select-Object -ExpandProperty Name)
git -C $wikiCheckout add -- $wikiFiles
git -C $wikiCheckout diff --cached --check
git -C $wikiCheckout commit -m "docs: publish reviewed MockX wiki"
if ($LASTEXITCODE -ne 0) { throw 'No wiki commit created; inspect Git output before pushing.' }
git -C $wikiCheckout -c credential.helper= -c 'credential.helper=!gh auth git-credential' push origin HEAD
if ($LASTEXITCODE -ne 0) { throw 'Wiki push failed; do not force-push.' }
```

If a concurrent update rejects the push, fetch and reconcile it before retrying. Never force-push the wiki or replace its entire history. Keep the local prepared source available if publication is blocked.

## Verify the live result

Open Home, the sidebar, Architecture, FIX Trading, REST API, and both C++ guides. Verify headings, tables, code blocks, Mermaid diagrams, and navigation render as intended. Follow the footer's maintenance link. Compare the published Markdown with `doc/wiki` and confirm no unrelated pages were removed.

Record the source PR, published wiki commit, test results, and any skipped rendering/native checks in the delivery message. This is a manual publication workflow; no background synchronization is installed.
