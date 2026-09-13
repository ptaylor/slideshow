# slideshow

Show the pictures in a directory as a slideshow in your browser.

`slideshow` scans a directory for images, starts a small web server bound to
`127.0.0.1` on a non-standard port, and opens a page showing one picture at a
time. The previous and next pictures appear as thumbnails to the left and
right, and the counter plus filename sit in a bar underneath the main image.

It is a single Python file using nothing but the standard library — there is
nothing to install and no build step.

## Install

```sh
./install.sh
```

This symlinks `${HOME}/bin/slideshow` to `slideshow.py` and makes the script
executable. If `${HOME}/bin` is not on your `PATH`, the script prints the exact
line to add to your `~/.zshrc`.

To install somewhere else:

```sh
BIN=/usr/local/bin ./install.sh
```

You can also skip installing and run it in place:

```sh
python3 slideshow.py ~/Pictures
```

## Usage

```
slideshow <DIR> [-d N] [-t SECONDS]
```

| Option | Meaning |
| --- | --- |
| `DIR` | Directory holding the images. Required. |
| `-d`, `--depth N` | How deep to look for images. `1` (the default) is `DIR` itself, `2` adds its immediate sub-directories, `3` adds one level below those, and so on. |
| `-t`, `--timeout SECONDS` | Automatically advance to the next image after `SECONDS`. Off by default. |

**Sub-directories are not searched unless you raise the depth.** `slideshow ~/Pictures`
shows only the files directly inside `~/Pictures`; `slideshow ~/Pictures -d 2`
also includes `~/Pictures/holiday/*.jpg`.

```sh
slideshow ~/Pictures                  # this directory only
slideshow ~/Pictures -d 2             # include immediate sub-directories
slideshow ~/Pictures --depth 4        # go four levels deep
slideshow ~/Pictures --timeout 5      # advance every 5 seconds
```

## Navigation

| Action | Result |
| --- | --- |
| `←` / `→` | Previous / next image |
| Click the left half of the screen | Previous image |
| Click the right half of the screen | Next image |
| Click a thumbnail | Jump to that image |
| `Space` | Pause / resume, only when `--timeout` is set |
| `Esc` | Stop the server |
| Click the **×** in the top-right corner | Stop the server |

The label bar shows the position, the file name, and any sub-directory path
relative to the directory you passed in:

```
12/87 — holiday/beach.jpg
```

Navigation stops at the first and last image rather than wrapping around. At
the ends the corresponding thumbnail slot dims, and the hint on that side is
suppressed.

## Ordering

Images are ordered by a case-insensitive *natural* sort of their path relative
to `DIR`, so digits compare as numbers rather than as text:

```
beach.png
IMG_0001.png
IMG_0002.png
IMG_0010.png
```

Because the comparison is on the relative path, each sub-directory's images
stay together as one group, placed where that directory name falls
alphabetically. For example, `holiday/` sorts between `beach.png` and
`IMG_0001.png`:

```
1  beach.png
2  holiday/city-lights.png
3  holiday/mountains.png
4  holiday/2023/archive-shot.png
5  IMG_0001.png
...
```

## Supported formats

`.jpg` `.jpeg` `.png` `.gif` `.webp` `.bmp` `.avif` `.svg` `.heic` `.heif` `.tif` `.tiff`

Files are matched case-insensitively (`.JPG` works). Hidden files and hidden
directories — anything starting with `.` — are skipped, as are symlinked
directories, so a symlink loop cannot hang the scan.

### HEIC, HEIF and TIFF

Chrome and Firefox cannot decode these formats, so they are shown as a
placeholder card stating the file name instead of a broken image. They are
still counted and ordered normally. Safari on macOS can display them, and
Preview opens them.

## Stopping the server

The server stops itself when you are finished with it. Any of these work:

- press `Esc`
- click the **×** button in the top-right corner
- close the browser tab
- press `Ctrl-C` in the terminal

Stopping it from the page replaces the picture with a "Server stopped" card and
prints the reason in the terminal, for example `slideshow: stopped (quit requested)`.

**Reloading the page does not stop the server.** Every page load registers a
random id, so the server can tell a reload — where the old page unloads
immediately before the new one appears — from a real close. It also waits a
couple of seconds before acting on a close, for the same reason. Because the
ids are tracked individually, closing one of several open tabs leaves the
server running for the others.

## Notes

- The server binds to `127.0.0.1` only, so nothing is exposed to your network.
- Images are addressed by their index in the scan, never by path, so a request
  cannot reach a file outside the directory you passed in.
- The first port tried is `8765`. If that is busy it works upwards (`8766`,
  `8767`, …) until it finds a free one.
- Thumbnails reuse the same URL as the main image, so the browser's cache means
  a picture is only fetched once as you navigate past it.
- Photos are displayed using their embedded EXIF orientation, which browsers
  apply automatically.
- Press `Ctrl-C` in the terminal to stop the server.

## Requirements

Python 3.9 or newer. No third-party packages.
