"""Extract palettes from a trusted checkout of Vim's runtime/colors directory.

This is a maintenance tool, never invoked by cagents. Run with --source pointing
at the official Vim sources and --revision giving their Git commit.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


EXPORT = """set nocompatible termguicolors background=dark
syntax on
execute 'source' fnameescape($CAGENTS_VIM_SOURCE)
let palette = {}
for group in ['Normal', 'Pmenu', 'PmenuSel', 'Visual', 'StatusLine', 'StatusLineNC', 'Comment', 'Statement', 'Identifier', 'ErrorMsg', 'WarningMsg', 'DiffAdd', 'MoreMsg', 'VertSplit', 'LineNr', 'Search']
  let id = synIDtrans(hlID(group))
  let palette[group] = {'fg': synIDattr(id, 'fg#', 'gui'), 'bg': synIDattr(id, 'bg#', 'gui'), 'reverse': synIDattr(id, 'reverse', 'gui') ==# '1'}
endfor
call writefile([json_encode({'dark': &background ==# 'dark', 'groups': palette})], $CAGENTS_VIM_OUTPUT)
qa!
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vim", default="vim")
    args = parser.parse_args()
    sources = sorted(args.source.glob("*.vim"))
    if not sources:
        parser.error("No Vim color schemes found")
    palettes = {}
    with tempfile.TemporaryDirectory(prefix="cagents-vim-export-") as directory:
        script, result = Path(directory) / "export.vim", Path(directory) / "result.json"
        script.write_text(EXPORT)
        for source in sources:
            subprocess.run([args.vim, "-Nu", "NONE", "-i", "NONE", "-n", "-es", "-S", str(script)],
                           env={**os.environ, "CAGENTS_VIM_SOURCE": str(source.resolve()),
                                "CAGENTS_VIM_OUTPUT": str(result)}, check=True, timeout=10)
            palettes[source.stem] = json.loads(result.read_text())
    data = {
        "commit": args.revision,
        "files": [path.name for path in sources],
        "url": f"https://github.com/vim/vim/tree/{args.revision}/runtime/colors",
        "palettes": palettes,
        "credits": {path.stem: "\n".join(line[1:].strip() for line in path.read_text().splitlines()[:12]
                                         if line.startswith('"') and not line.startswith('" Generated'))
                    for path in sources},
    }
    args.output.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
