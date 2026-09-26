# AGENTS.md

`slideshow` shows the pictures in a directory as a slideshow in the browser. It
scans a directory, serves the images from `127.0.0.1`, and opens one page that
shows them with keyboard, mouse and optional autoplay.

`README.md` is the user-facing contract. Read it before changing behaviour, and
keep it in step with what you change.

## Hard constraints

- **One file, no dependencies, no build step.** Everything lives in
  `slideshow.py`: the CLI, the scanner, the HTTP server, and the entire front end
  as `PAGE_CSS`, `ICON_SVG`, `PAGE_JS` and `PAGE_TEMPLATE`. Do not add a
  `static/` folder, a second module, a third-party import, or a package layout.
  `install.sh` does nothing but symlink the one file into `${HOME}/bin`.
- **Python 3.9 or newer.** Keep `from __future__ import annotations`, and prefer
  the `typing.List` / `Optional` / `Tuple` spellings over newer builtins.
- **Images are addressed by index** (`/image/<int>`), never by path. That is the
  directory-traversal defence — do not swap it for a path-based route.
- **`icon.svg` and `ICON_SVG` are the same artwork.** Change both together, or
  the README icon and the favicon drift apart.

## Layout

| Area | Symbols |
| --- | --- |
| CLI | `build_parser`, `positive_int`, `positive_float`, `main` |
| Scanning and ordering | `natural_key`, `scan_images`, `ImageEntry` |
| HTTP | `SlideshowServer`, `build_handler` (its nested `SlideshowHandler`), `create_server` |
| Page lifecycle | `PageRegistry` — stops the server once the last viewer page is gone |
| Front end | `PAGE_CSS`, `ICON_SVG`, `PAGE_JS`, `PAGE_TEMPLATE`, `PAGE`, `render_page` |

Two things bite when editing the front end:

- `PAGE` is assembled at **import** time, so a server that is already running
  keeps serving the old HTML. Restart it after touching `PAGE_CSS`, `PAGE_JS` or
  `PAGE_TEMPLATE`.
- The config reaches the page as a JSON literal substituted for the
  `"__SLIDESHOW_CONFIG__"` placeholder, with `</` escaped to `<\/`. Keep that
  escaping, or a file named `evil</script>.jpg` can break out of the `<script>`
  tag.

## Checks before committing

There is no test suite. Both of these must pass, and the second is not optional:
**`py_compile` does not look at the embedded JavaScript.**

```sh
python3 -m py_compile slideshow.py

python3 -c 'import importlib.util, sys; s = importlib.util.spec_from_file_location("ss", "slideshow.py"); m = importlib.util.module_from_spec(s); sys.modules["ss"] = m; s.loader.exec_module(m); open("/tmp/slideshow-page.js", "w").write(m.PAGE_JS)' \
  && node --check /tmp/slideshow-page.js
```

Then look at it: `python3 slideshow.py <DIR> --timeout 5` and exercise the thing
you changed in the browser. The layout code depends on CSS that no compiler
checks, and the grid collapses in ways that are only visible when rendered.

## Conventions

- Comments explain **why**, not what. The existing ones record the trap that was
  avoided — `PageRegistry`, `scan_images` and the `.frame` / `.stage` CSS are
  good models.
- Commit messages are imperative sentences with no conventional-commit prefix:
  "Add a copy button for the full image path".
- A behaviour change usually touches four places: the code, the `--help` epilog
  in `build_parser`, the module docstring, and `README.md`.

## AI attribution — required

Any commit produced with an AI coding assistant — for the code, the docs, the
artwork or the commit message itself — must end with a trailer that names the
assistant **and** the exact model and version:

```
Assisted-by: <assistant> (<model> <version>)
```

```
Assisted-by: GitHub Copilot (DeepSeek V4 Flash)
```

Rules:

- Name the model you were actually running, and its version when it has one.
  "AI", "Copilot", "an LLM" or a bare tool name is not attribution.
- One trailer per assistant: two assistants on one commit means two
  `Assisted-by:` trailers.
- Use `git commit --trailer "Assisted-by=GitHub Copilot (DeepSeek V4 Flash)"`
  rather than hand-placing the line, so it lands in the trailer block.
- No trailer means the work was written by hand. Never add one for a change you
  did not make.
