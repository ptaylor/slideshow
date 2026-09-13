#!/usr/bin/env python3
"""slideshow - view a directory of images as a local browser slideshow.

    slideshow <DIR> [--depth N] [--timeout SECONDS]

The directory is scanned for image files, a small HTTP server is started on
127.0.0.1 using a non-standard port, and a browser page is opened showing the
images one at a time. The previous and next images are shown as thumbnails to
the left and right of the main picture.

Standard library only - there is nothing to install.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".avif",
        ".svg",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
    }
)

# Formats that no mainstream desktop browser can decode from a plain <img>.
# Safari on macOS can display HEIC/HEIF/TIFF; Chrome and Firefox cannot. These
# files are still scanned, counted and ordered, but render as a placeholder
# card with an explanation rather than a broken image.
BROWSER_UNSUPPORTED = frozenset({".heic", ".heif", ".tif", ".tiff"})

# Content types are pinned rather than guessed, because whether the platform
# mime database knows about .webp/.avif/.heic varies between systems.
MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_SCAN_LIMIT = 100
CHUNK_SIZE = 64 * 1024

# When the last viewer page says goodbye, wait this long before stopping, so
# that a reload (which closes the old page before opening the new one) does not
# kill the server.
CLOSE_GRACE_SECONDS = 2.0

# After an explicit quit, give the HTTP response a moment to reach the browser
# before the socket is torn down.
QUIT_GRACE_SECONDS = 0.25

# A natural sort key for one path component, e.g. "photo10" ->
# ((0, "photo"), (1, 10)). The leading integer tag guarantees we never compare
# a string against an int, which would raise a TypeError.
NaturalKey = Tuple[Tuple[int, object], ...]
PathKey = Tuple[NaturalKey, ...]

_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> NaturalKey:
    """Return a sort key giving human ordering (photo2 before photo10)."""
    key: List[Tuple[int, object]] = []
    for part in _DIGITS.split(text):
        if part.isdigit():
            key.append((1, int(part)))
        elif part:
            key.append((0, part.casefold()))
    return tuple(key)


@dataclass(frozen=True)
class ImageEntry:
    """One image discovered by :func:`scan_images`."""

    index: int
    abs_path: Path
    rel_path: str
    name: str
    unsupported: bool


def scan_images(root: Path, depth: int) -> Tuple[List[ImageEntry], List[str]]:
    """Collect images under *root*, recursing at most *depth* levels.

    ``depth=1`` means the directory itself only, ``depth=2`` includes its
    immediate sub-directories, and so on.

    Returns the sorted entries together with a list of non-fatal warnings
    (for example directories that could not be read).
    """
    warnings: List[str] = []
    found: List[Tuple[PathKey, Path]] = []

    def on_error(error: OSError) -> None:
        warnings.append(str(error))

    # followlinks=False means symlinked directories are listed but never
    # traversed, so a symlink loop cannot hang the scan.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
        current = Path(dirpath)
        level = 0 if current == root else len(current.relative_to(root).parts)

        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        if level + 1 >= depth:
            dirnames[:] = []  # deepest requested level, so stop descending

        for filename in filenames:
            if filename.startswith("."):
                continue
            if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            rel_path = (current.relative_to(root) / filename).as_posix()
            # Sorting per path component keeps files inside one directory
            # together instead of sorting "a/b.jpg" against "a-b.jpg".
            found.append((tuple(natural_key(part) for part in Path(rel_path).parts), rel_path))

    found.sort(key=lambda item: item[0])

    entries = [
        ImageEntry(
            index=index,
            abs_path=root / rel_path,
            rel_path=rel_path,
            name=Path(rel_path).name,
            unsupported=Path(rel_path).suffix.lower() in BROWSER_UNSUPPORTED,
        )
        for index, (_, rel_path) in enumerate(found)
    ]
    return entries, warnings


# --------------------------------------------------------------------------
# Front end (embedded so that slideshow.py stays a single file)
# --------------------------------------------------------------------------

PAGE_CSS = r"""
:root {
  --bg: #0c0e11;
  --panel: #151a20;
  --edge: rgba(255, 255, 255, 0.08);
  --fg: #e7ebf1;
  --muted: #8d97a5;
  --accent: #63a4ff;
  --rail: clamp(72px, 13vw, 190px);
}

* { box-sizing: border-box; }

html, body { height: 100%; margin: 0; }

body {
  background: radial-gradient(1200px 800px at 50% -10%, #1b222b 0%, var(--bg) 62%);
  color: var(--fg);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  overflow: hidden;
  -webkit-user-select: none;
  user-select: none;
  cursor: default;
}

#app {
  display: grid;
  grid-template-columns: var(--rail) minmax(0, 1fr) var(--rail);
  gap: 18px;
  height: 100%;
  padding: 20px;
}

.rail {
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
}

main {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  min-height: 0;
}

/* The stage is the area left over above the label bar. The frame is taken out
   of flow so its height does not depend on the picture, and centred with flex
   rather than grid: a percentage max-height on the image needs a *definite*
   containing block, and a grid auto-row is content-sized, so the percentage
   would be ignored and a tall picture would balloon past the label bar.
   overflow:hidden is belt and braces - nothing can paint over the label bar
   even if a browser resolves the percentage differently. */
.stage {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}

.frame {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.frame img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 10px;
  box-shadow: 0 6px 22px rgba(0, 0, 0, 0.45);
  -webkit-user-drag: none;
  animation: fade 0.18s ease both;
}

@keyframes fade { from { opacity: 0.35; } to { opacity: 1; } }

/* Neighbour thumbnails -------------------------------------------------- */

.thumb {
  appearance: none;
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: 100%;
  max-height: 62vh;
  padding: 5px;
  border: 1px solid var(--edge);
  border-radius: 11px;
  background: #090b0e;
  color: var(--muted);
  font: inherit;
  cursor: pointer;
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
}

.thumb img {
  display: block;
  max-width: 100%;
  max-height: 62vh;
  border-radius: 7px;
  object-fit: contain;
  -webkit-user-drag: none;
}

.thumb:hover,
.thumb:focus-visible {
  border-color: rgba(99, 164, 255, 0.55);
  box-shadow: 0 0 0 3px rgba(99, 164, 255, 0.14);
  transform: scale(1.03);
}

.thumb:focus { outline: none; }
.thumb:focus-visible { outline: none; }

.thumb-ph {
  display: block;
  max-width: 100%;
  padding: 14px 6px;
  font-size: 11px;
  line-height: 1.3;
  text-align: center;
  overflow-wrap: anywhere;
}

.rail.empty { opacity: 0.25; }

/* Image label ----------------------------------------------------------- */

.labelbar {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: baseline;
  gap: 9px;
  flex: 0 0 auto;
  min-width: 0;
  padding: 9px 15px;
  border: 1px solid var(--edge);
  border-radius: 11px;
  background: var(--panel);
}

.counter {
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
  font-weight: 650;
  letter-spacing: 0.01em;
}

.sep { flex: 0 0 auto; color: var(--muted); }

.dirpath {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.filename {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.autostate {
  flex: 0 0 auto;
  padding-left: 10px;
  color: var(--accent);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

/* Copy button ------------------------------------------------------------ */

.copy {
  display: grid;
  place-items: center;
  align-self: center;
  flex: 0 0 auto;
  /* Deterministic right alignment: .dirpath grows to absorb the free space
     when the picture has a sub-directory, and this auto margin covers the
     case where it is hidden and nothing else can grow. */
  margin-left: auto;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--edge);
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  transition: color 0.16s ease, border-color 0.16s ease, background 0.16s ease;
}

.copy svg {
  display: block;
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.copy:hover,
.copy:focus-visible {
  color: var(--fg);
  border-color: rgba(99, 164, 255, 0.55);
  background: rgba(99, 164, 255, 0.12);
}

.copy:focus { outline: none; }

.copy .icon-done { display: none; }

.copy.copied {
  color: #7ce3b2;
  border-color: rgba(124, 227, 178, 0.55);
  background: rgba(124, 227, 178, 0.12);
}

.copy.copied .icon-copy { display: none; }
.copy.copied .icon-done { display: block; }

/* Formats the browser cannot decode ------------------------------------- */

.unsupported {
  grid-area: 1 / 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  max-width: min(560px, 82%);
  padding: 30px 26px;
  border: 1px dashed rgba(255, 255, 255, 0.18);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.025);
  text-align: center;
}

.unsupported .icon { font-size: 34px; line-height: 1; opacity: 0.75; }

.unsupported .fname {
  font-weight: 600;
  overflow-wrap: anywhere;
}

.unsupported .msg { margin: 4px 0 0; }

.unsupported .hint {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}

/* Left / right half click hints ----------------------------------------- */

.halfhint {
  position: fixed;
  top: 0;
  bottom: 0;
  width: min(26vw, 300px);
  display: grid;
  place-items: center;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s ease;
}

.halfhint span {
  color: var(--fg);
  font-size: 40px;
  line-height: 1;
  opacity: 0.5;
}

.halfhint.left {
  left: 0;
  background: linear-gradient(to right, rgba(99, 164, 255, 0.1), transparent);
}

.halfhint.right {
  right: 0;
  background: linear-gradient(to left, rgba(99, 164, 255, 0.1), transparent);
}

body.hint-left:not(.at-start) .halfhint.left,
body.hint-right:not(.at-end) .halfhint.right { opacity: 1; }

/* Stop button ------------------------------------------------------------ */

.quit {
  position: fixed;
  top: 14px;
  right: 14px;
  z-index: 5;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--edge);
  border-radius: 50%;
  background: rgba(21, 26, 32, 0.85);
  color: var(--muted);
  font: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  opacity: 0.6;
  transition: opacity 0.16s ease, color 0.16s ease, border-color 0.16s ease,
    background 0.16s ease;
}

.quit:hover,
.quit:focus-visible {
  opacity: 1;
  color: #ff9b9b;
  border-color: rgba(255, 155, 155, 0.55);
  background: rgba(46, 22, 26, 0.92);
}

.quit:focus { outline: none; }

/* Stopped state ---------------------------------------------------------- */

/* Collapse to a single column: hiding the rails with display:none removes them
   from grid auto-placement, which would otherwise pull <main> into the left
   rail's 110px track. */
body.stopped #app { grid-template-columns: minmax(0, 1fr); }

body.stopped .rail,
body.stopped .halfhint,
body.stopped .labelbar,
body.stopped .quit { display: none; }

.stopped-card {
  grid-area: 1 / 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 34px 30px;
  border: 1px solid var(--edge);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.025);
  text-align: center;
}

.stopped-card .icon { font-size: 22px; line-height: 1; color: var(--muted); }
.stopped-card .msg { margin: 0; font-weight: 600; }
.stopped-card .hint { margin: 0; color: var(--muted); font-size: 13px; }

@media (max-width: 700px) {
  :root { --rail: clamp(56px, 19vw, 110px); }
  #app { gap: 10px; padding: 12px; }
  .halfhint { width: 34vw; }
  .halfhint span { font-size: 26px; }
}

@media (prefers-reduced-motion: reduce) {
  .thumb, .halfhint, .frame img { transition: none; animation: none; }
}
"""

PAGE_JS = r"""
(() => {
  "use strict";

  const items = CONFIG.items;
  const total = items.length;

  const frame = document.getElementById("frame");
  const counter = document.getElementById("counter");
  const dirEl = document.getElementById("dirpath");
  const fileEl = document.getElementById("filename");
  const labelbar = document.getElementById("labelbar");
  const railPrev = document.getElementById("rail-prev");
  const railNext = document.getElementById("rail-next");
  const autoEl = document.getElementById("autostate");
  const quitButton = document.getElementById("quit");
  const copyButton = document.getElementById("copy");

  // A fresh id per page load: the server uses it to tell a reload (close then
  // open, same browser) from a genuine tab close.
  const pageId = Math.random().toString(36).slice(2) + Date.now().toString(36);

  // The markup owns the wording; this is just what to restore after a copy.
  const copyTitle = copyButton.title;

  let current = 0;
  let paused = false;
  let finished = false;
  let stopped = false;
  let timer = null;
  let copyTimer = null;

  const urlFor = (index) => "/image/" + index + "?v=" + CONFIG.token;

  // The manifest stores paths relative to the directory, so join the root back
  // on to give something that can be pasted into a terminal or Finder.
  const absolutePath = (item) =>
    CONFIG.root.replace(/\/+$/, "") + "/" + item.path;

  const copyText = async (text) => {
    // 127.0.0.1 counts as a secure context, so the async clipboard is normally
    // available; the textarea path is a fallback for anything older.
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (error) {
      /* fall through */
    }
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      document.body.append(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch (error) {
      return false;
    }
  };

  const post = (route, body) => {
    try {
      fetch(route, { method: "POST", body: body, keepalive: true, cache: "no-store" });
    } catch (error) {
      /* the server may already be gone */
    }
  };

  const makeImage = (item, index) => {
    const img = document.createElement("img");
    img.src = urlFor(index);
    img.alt = item.path;
    img.draggable = false;
    img.decoding = "async";
    return img;
  };

  const makePlaceholder = (item, compact) => {
    const card = document.createElement("div");
    card.className = compact ? "thumb-ph" : "unsupported";
    if (compact) {
      card.textContent = item.name;
      return card;
    }
    const icon = document.createElement("div");
    icon.className = "icon";
    icon.textContent = "\uD83D\uDDBC";
    const name = document.createElement("div");
    name.className = "fname";
    name.textContent = item.name;
    const msg = document.createElement("p");
    msg.className = "msg";
    msg.textContent = "This format cannot be previewed in this browser.";
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "HEIC, HEIF and TIFF files open fine in Safari or Preview.";
    card.append(icon, name, msg, hint);
    return card;
  };

  const fillRail = (rail, index) => {
    rail.replaceChildren();
    if (index < 0 || index >= total) {
      rail.classList.add("empty");
      return;
    }
    rail.classList.remove("empty");

    const item = items[index];
    const button = document.createElement("button");
    button.type = "button";
    button.className = "thumb";
    button.dataset.index = String(index);
    button.title = item.path;
    button.setAttribute("aria-label", "Image " + (index + 1) + " of " + total + ": " + item.path);
    button.append(
      item.unsupported ? makePlaceholder(item, true) : makeImage(item, index)
    );
    rail.append(button);
  };

  const render = () => {
    const item = items[current];

    frame.replaceChildren(
      item.unsupported ? makePlaceholder(item, false) : makeImage(item, current)
    );

    counter.textContent = (current + 1) + "/" + total;

    const parts = item.path.split("/");
    const fileName = parts.pop();
    dirEl.textContent = parts.length ? parts.join("/") + "/" : "";
    dirEl.hidden = parts.length === 0;
    fileEl.textContent = fileName;
    labelbar.title = item.path;

    fillRail(railPrev, current - 1);
    fillRail(railNext, current + 1);

    document.body.classList.toggle("at-start", current === 0);
    document.body.classList.toggle("at-end", current === total - 1);
    document.title = (current + 1) + "/" + total + " \u2014 " + item.path;

    // Warm the browser cache for the two neighbours.
    [current - 1, current + 1].forEach((index) => {
      if (index >= 0 && index < total && !items[index].unsupported) {
        const preload = new Image();
        preload.src = urlFor(index);
      }
    });
  };

  const schedule = () => {
    // Clear first: a pending timer must be cancelled when pausing, otherwise
    // the already-queued tick still fires and advances one more image.
    clearTimeout(timer);
    if (stopped || !CONFIG.timeout || paused || finished) return;
    timer = setTimeout(() => {
      if (current + 1 >= total) {
        finished = true;
        updateAutoState();
        return;
      }
      current += 1;
      render();
      schedule();
    }, CONFIG.timeout * 1000);
  };

  const updateAutoState = () => {
    if (!CONFIG.timeout) {
      autoEl.hidden = true;
      return;
    }
    autoEl.hidden = false;
    if (finished) autoEl.textContent = "finished";
    else if (paused) autoEl.textContent = "\u23F8 paused";
    else autoEl.textContent = "\u25B6 " + CONFIG.timeout + "s";
  };

  const go = (target) => {
    if (target < 0 || target >= total || target === current) return;
    current = target;
    if (finished) finished = false;
    render();
    updateAutoState();
    schedule();
  };

  const step = (delta) => go(current + delta);

  const showStopped = () => {
    document.title = "slideshow stopped";
    document.body.classList.add("stopped");
    document.body.classList.remove("hint-left", "hint-right");
    const card = document.createElement("div");
    card.className = "stopped-card";
    const icon = document.createElement("div");
    icon.className = "icon";
    icon.textContent = "\u25A0";
    const msg = document.createElement("p");
    msg.className = "msg";
    msg.textContent = "Server stopped";
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "You can close this tab.";
    card.append(icon, msg, hint);
    frame.replaceChildren(card);
    counter.textContent = "";
    dirEl.textContent = "";
    fileEl.textContent = "";
    autoEl.hidden = true;
  };

  const stopServer = () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(timer);
    post("/quit", pageId);
    showStopped();
  };

  quitButton.addEventListener("click", (event) => {
    event.preventDefault();
    stopServer();
  });

  const copyCurrentPath = async () => {
    if (stopped) return;
    const ok = await copyText(absolutePath(items[current]));
    copyButton.classList.toggle("copied", ok);
    copyButton.title = ok ? "Copied" : "Copy failed";
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copyButton.classList.remove("copied");
      copyButton.title = copyTitle;
    }, 1400);
  };

  copyButton.addEventListener("click", (event) => {
    event.preventDefault();
    copyCurrentPath();
  });

  document.addEventListener("click", (event) => {
    if (stopped) return;
    const target = event.target instanceof Element ? event.target : null;

    const thumb = target ? target.closest("[data-index]") : null;
    if (thumb) {
      go(Number(thumb.dataset.index));
      return;
    }
    // Thumbnails are handled above. Everything else that is a control - the
    // copy button, the quit button - and the whole label bar must not double
    // as a navigation click, or a near miss on the small copy button would
    // advance the slideshow.
    if (target && target.closest("button, #labelbar")) return;

    step(event.clientX < window.innerWidth / 2 ? -1 : 1);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      stopServer();
      return;
    }
    if (stopped) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      step(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      step(1);
    } else if ((event.key === "c" || event.key === "C")
               && !event.metaKey && !event.ctrlKey) {
      // Leave Cmd-C alone so the browser can copy a text selection.
      event.preventDefault();
      copyCurrentPath();
    } else if (event.key === " " || event.key === "Spacebar") {
      if (!CONFIG.timeout) return;
      event.preventDefault();
      paused = !paused;
      if (!paused) finished = false;
      updateAutoState();
      schedule();
    }
  });

  let hint = "";
  document.addEventListener("mousemove", (event) => {
    if (stopped) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target && target.closest(".quit, .copy")) {
      hint = "";
      document.body.classList.remove("hint-left", "hint-right");
      return;
    }
    const side = event.clientX < window.innerWidth / 2 ? "left" : "right";
    if (side === hint) return;
    hint = side;
    document.body.classList.toggle("hint-left", side === "left");
    document.body.classList.toggle("hint-right", side === "right");
  });

  // Register this page, and tell the server when it goes away. pagehide fires
  // for a tab close, a navigation and a reload, which is why the server waits
  // briefly before acting on it.
  post("/open", pageId);

  window.addEventListener("pagehide", (event) => {
    if (stopped || event.persisted) return; // bfcache: the page comes back
    if (navigator.sendBeacon) navigator.sendBeacon("/close", pageId);
    else post("/close", pageId);
  });

  window.addEventListener("pageshow", (event) => {
    if (event.persisted && !stopped) post("/open", pageId);
  });

  render();
  updateAutoState();
  schedule();
})();
"""

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>slideshow</title>
<style>__CSS__</style>
</head>
<body>
<div id="app">
  <aside class="rail" id="rail-prev" aria-label="Previous image"></aside>
  <main>
    <div class="stage"><div class="frame" id="frame"></div></div>
    <div class="labelbar" id="labelbar" aria-live="polite">
      <span class="counter" id="counter">&ndash;/&ndash;</span>
      <span class="sep" aria-hidden="true">&mdash;</span>
      <span class="dirpath" id="dirpath"></span>
      <span class="filename" id="filename"></span>
      <span class="autostate" id="autostate" hidden></span>
      <button class="copy" id="copy" type="button" title="Copy the full path (C)"
              aria-label="Copy the full path of this image">
        <svg class="icon-copy" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
        </svg>
        <svg class="icon-done" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <polyline points="20 6 9 17 4 12"></polyline>
        </svg>
      </button>
    </div>
  </main>
  <aside class="rail" id="rail-next" aria-label="Next image"></aside>
</div>
<div class="halfhint left" aria-hidden="true"><span>&lsaquo;</span></div>
<div class="halfhint right" aria-hidden="true"><span>&rsaquo;</span></div>
<button class="quit" id="quit" type="button" title="Stop the server (Esc)"
        aria-label="Stop the slideshow server">&#215;</button>
<script>
const CONFIG = "__SLIDESHOW_CONFIG__";
__JS__
</script>
</body>
</html>
"""

# The CSS/JS substitution happens once, at import time.
PAGE = PAGE_TEMPLATE.replace("__CSS__", PAGE_CSS).replace("__JS__", PAGE_JS)


def render_page(
    entries: Sequence[ImageEntry], timeout: Optional[float], root: Path, cache_token: str
) -> bytes:
    """Build the viewer page with the image manifest baked in.

    *cache_token* is per-run and goes into every image URL. Without it, two
    runs serving different directories on the same port would both use
    ``/image/0``, and the browser would reuse the first run's cached picture.
    """
    config = {
        "timeout": timeout,
        "token": cache_token,
        # Sent once rather than per item: repeating an absolute prefix across
        # thousands of entries bloats the page for no benefit.
        "root": str(root),
        "items": [
            {"name": entry.name, "path": entry.rel_path, "unsupported": entry.unsupported}
            for entry in entries
        ],
    }
    payload = json.dumps(config, ensure_ascii=False)
    # A filename containing "</script>" must not be able to close the tag.
    payload = payload.replace("</", "<\\/")
    return PAGE.replace('"__SLIDESHOW_CONFIG__"', payload).encode("utf-8")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class SlideshowServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Do not wait for in-flight transfers when shutting down, so Ctrl-C is
    # always immediate.
    block_on_close = False

    def handle_error(self, request: object, client_address: object) -> None:
        # The close beacon fires while a page is unloading, so the browser may
        # drop the connection before the reply is written. Not worth a
        # traceback in the terminal.
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class PageRegistry:
    """Stop the server once every viewer page has gone away.

    Each page load gets its own id. That matters because a reload closes the
    old page before the new one opens, so a simple "last page closed" flag
    would shut the server down on every refresh. Tracking ids also means that
    closing one of several open tabs leaves the server running for the rest.
    """

    def __init__(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        self._grace = grace
        self._lock = threading.Lock()
        self._pages: Set[str] = set()
        self._timer: Optional[threading.Timer] = None
        self._server: Optional[SlideshowServer] = None
        # Why the server is stopping, for the message printed on exit.
        self.reason: Optional[str] = None

    def attach(self, server: SlideshowServer) -> None:
        self._server = server

    def opened(self, page_id: str) -> None:
        with self._lock:
            self._pages.add(page_id)
            self._cancel_timer()

    def closed(self, page_id: str) -> None:
        with self._lock:
            self._pages.discard(page_id)
            if self._pages:
                return
            # Do not stop immediately: a reload reports the old page closed
            # just before the new page checks in.
            self._cancel_timer()
            self._timer = threading.Timer(self._grace, self._after_grace)
            self._timer.daemon = True
            self._timer.start()

    def quit_now(self) -> None:
        with self._lock:
            self._cancel_timer()
            self._stop("quit requested")

    def _after_grace(self) -> None:
        with self._lock:
            self._timer = None
            if self._pages:
                return  # a page checked back in, so the reload is continuing
            self._stop("page closed")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _stop(self, reason: str) -> None:
        server = self._server
        if server is None or self.reason is not None:
            return
        self.reason = reason

        def teardown() -> None:
            # shutdown() blocks until serve_forever() returns, so it must never
            # run on a request thread.
            time.sleep(QUIT_GRACE_SECONDS)
            server.shutdown()

        threading.Thread(target=teardown, daemon=True).start()


def build_handler(
    entries: Sequence[ImageEntry], page: bytes, registry: PageRegistry
) -> Callable[..., BaseHTTPRequestHandler]:
    """Create a request handler bound to a fixed set of images."""

    class SlideshowHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "slideshow"
        sys_version = ""

        def log_message(self, fmt: str, *args: object) -> None:
            pass  # keep the terminal quiet; errors surface via the CLI

        def _send(self, status: int, body: bytes, content_type: str, cache: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_ok(self) -> None:
            self._send(200, b'{"ok":true}', "application/json", "no-store")

        def _read_body(self) -> str:
            """Drain the request body, then decode it.

            The body must always be read, even for routes that ignore it, or
            the next request on a keep-alive connection starts mid-stream.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return ""
            return self.rfile.read(length).decode("utf-8", "replace")

        def _send_image(self, raw_index: str) -> None:
            try:
                index = int(raw_index)
            except ValueError:
                self.send_error(404, "No such image")
                return
            if not 0 <= index < len(entries):
                self.send_error(404, "No such image")
                return

            entry = entries[index]
            try:
                handle = open(entry.abs_path, "rb")
            except OSError:
                self.send_error(404, "Image unavailable")
                return

            with handle:
                try:
                    size = os.fstat(handle.fileno()).st_size
                except OSError:
                    self.send_error(500, "Cannot stat image")
                    return

                suffix = Path(entry.name).suffix.lower()
                content_type = MIME_TYPES.get(suffix) or mimetypes.guess_type(entry.name)[0]
                self.send_response(200)
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()

                written = 0
                while written < size:
                    chunk = handle.read(min(CHUNK_SIZE, size - written))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    written += len(chunk)

                if written != size:
                    # The file changed underneath us; do not leave the
                    # connection open with a mismatched Content-Length.
                    self.close_connection = True

        def do_GET(self) -> None:
            route = urlparse(self.path).path

            if route in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8", "no-store")
            elif route.startswith("/image/"):
                self._send_image(route[len("/image/") :])
            elif route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404, "Not found")

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            page_id = self._read_body().strip()

            if route == "/open":
                if page_id:
                    registry.opened(page_id)
                self._send_ok()
            elif route == "/close":
                registry.closed(page_id)
                self._send_ok()
            elif route == "/quit":
                # Respond first so the browser sees the acknowledgement, then
                # let the registry tear the server down. The finally block
                # matters: the client may vanish mid-reply, and the quit must
                # still happen.
                try:
                    self._send_ok()
                finally:
                    registry.quit_now()
            else:
                self.send_error(404, "Not found")

    return SlideshowHandler


def create_server(
    handler: Callable[..., BaseHTTPRequestHandler], preferred_port: int
) -> Tuple[SlideshowServer, int]:
    """Bind the first free port at or after *preferred_port*."""
    last_error: Optional[OSError] = None
    for candidate in list(range(preferred_port, preferred_port + PORT_SCAN_LIMIT)) + [0]:
        try:
            server = SlideshowServer((HOST, candidate), handler)
        except OSError as error:
            last_error = error
            continue
        return server, int(server.server_address[1])
    raise RuntimeError(f"could not bind a port on {HOST}: {last_error}")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return value


def positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slideshow",
        description="Show the images in a directory as a slideshow in your browser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "navigation:\n"
            "  left / right arrow keys     previous / next image\n"
            "  click the left screen half  previous image\n"
            "  click the right screen half next image\n"
            "  click a thumbnail           jump to that image\n"
            "  space                       pause / resume --timeout\n"
            "\n"
            "copying:\n"
            "  c, or the copy button       copy the full path of this image\n"
            "\n"
            "stopping:\n"
            "  Esc, the x button, or closing the tab stops the server\n"
            "  Ctrl-C in this terminal also stops it\n"
            "\n"
            "examples:\n"
            "  slideshow\n"
            "  slideshow ~/Pictures\n"
            "  slideshow ~/Pictures -d 2\n"
            "  slideshow ~/Pictures --depth 3 --timeout 5\n"
        ),
    )
    parser.add_argument(
        "directory",
        metavar="DIR",
        nargs="?",
        default=".",
        help="directory holding the images (default: the current directory)",
    )
    parser.add_argument(
        "-d",
        "--depth",
        type=positive_int,
        default=1,
        metavar="N",
        help=(
            "how deep to look for images: 1 (default) is DIR itself, "
            "2 adds its immediate sub-directories, and so on. "
            "Sub-directories are never searched unless this is greater than 1."
        ),
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=None,
        metavar="SECONDS",
        help="automatically advance to the next image after SECONDS (space pauses)",
    )
    return parser


def _install_signal_handlers() -> None:
    def stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is None:
            continue
        try:
            signal.signal(handler, stop)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.directory).expanduser()
    if not root.exists():
        print(f"slideshow: no such directory: {args.directory}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"slideshow: not a directory: {args.directory}", file=sys.stderr)
        return 2
    root = root.resolve()

    entries, warnings = scan_images(root, args.depth)
    for warning in warnings:
        print(f"slideshow: warning: {warning}", file=sys.stderr)

    if not entries:
        printable = " ".join(sorted(extension.lstrip(".") for extension in SUPPORTED_EXTENSIONS))
        print(f"slideshow: no images found in {root} (depth {args.depth})", file=sys.stderr)
        print(f"slideshow: looking for: {printable}", file=sys.stderr)
        if args.depth == 1:
            print(
                "slideshow: sub-directories were not searched"
                " - try --depth 2 to include them",
                file=sys.stderr,
            )
        return 1

    page = render_page(entries, args.timeout, root, secrets.token_hex(4))
    registry = PageRegistry()
    handler = build_handler(entries, page, registry)

    try:
        server, port = create_server(handler, DEFAULT_PORT)
    except RuntimeError as error:
        print(f"slideshow: {error}", file=sys.stderr)
        return 1

    registry.attach(server)

    url = f"http://{HOST}:{port}/"
    summary = f"slideshow: {len(entries)} image(s) from {root} (depth {args.depth})"
    if args.timeout:
        summary += f", auto-advance every {args.timeout:g}s"
    print(summary)
    print(f"slideshow: {url}")
    print("slideshow: Esc or the x button in the page stops this, or Ctrl-C here")

    _install_signal_handlers()
    # Delay the browser slightly so the socket is definitely listening first.
    threading.Timer(0.3, webbrowser.open, [url]).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nslideshow: stopped")
    else:
        print(f"\nslideshow: stopped ({registry.reason})")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
