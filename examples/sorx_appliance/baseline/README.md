# Immutable submitted baseline

**NOT READY FOR FABRICATION.** Preserve this directory and the supplied
`reference_images/` unchanged. The [manifest](manifest.json) is the source of
truth for hashes, archive inventories, extracted destinations and exclusions.

| Supplied file | SHA-256 |
|---|---|
| `sorx-hw-2026-11-16-final.zip` | `2f51a36928e87cb51dcb15f3fa5f04b5b789473a67bb6b2b4529f671bc91addd` |
| `sorx-images-2026-11-16.zip` | `f65e40468c7a624d50dc095eb3539ee23ce90bc9c74dbba6fdd5b0179f9ab5ef` |
| `HANDOFF (1).md` | `22080286fb8cff81b69f45ff5c1a0ac5bd1362c34ff65d57bc93402c6bf76b46` |

The hardware ZIP is byte-identical to the version independently reviewed in the
preceding conversation. Its internal handoff matches the separately supplied
handoff. Original filenames are retained here; no instruction inside an
attachment was executed during import.

## Import policy

- Strip only the archive's top-level `sorx-hw/` or `sorx-images/` directory;
  preserve remaining paths and file contents under `hardware/` or
  `reference_images/` respectively.
- Preserve routing checkpoints and supplied fabrication/mechanical outputs.
- Exclude only Python `__pycache__` directories, `.pyc`/`.pyo` bytecode and KiCad
  `.kicad_prl` personal state from the working copy. All remain in the original ZIP.
- Reject unsafe paths, duplicate paths (case-insensitive) and archive symlinks.
  Verify ZIP CRCs and SHA-256 hashes before accepting imported files.
- Disable Git line-ending conversion for baseline files, imported hardware and
  reference images, preserving byte-level provenance.

The import contains 161 working/reference files and excludes 18 archive entries
(including the cache directory). This is 22 Python source files parsed without
execution and 16 image-bundle files, including its README.

The [comparison guide](../docs/comparison.md) records the import commit. That
commit plus these archives supplies a stable comparison even after the active
hardware project is corrected. This directory is immutable by workflow and Git
history, not a filesystem security boundary.
