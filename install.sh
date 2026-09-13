#!/bin/sh
#
# Install the `slideshow` command by symlinking it into ${HOME}/bin.
#
#   ./install.sh                 # install into ${HOME}/bin
#   BIN=/usr/local/bin ./install.sh   # install somewhere else
#
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN="${BIN:-${HOME}/bin}"
SCRIPT="${REPO_DIR}/slideshow.py"
LINK="${BIN}/slideshow"

if [ ! -f "$SCRIPT" ]; then
    echo "install.sh: cannot find ${SCRIPT}" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "install.sh: python3 was not found on PATH" >&2
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
PY_SUPPORTED=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')

if [ "$PY_SUPPORTED" != "1" ]; then
    echo "install.sh: python3 ${PY_VERSION} was found, but 3.9 or newer is required" >&2
    exit 1
fi

mkdir -p "$BIN"
chmod +x "$SCRIPT"

# -f replaces any stale symlink, -n replaces a symlink to a directory.
ln -sfn "$SCRIPT" "$LINK"

echo "Installed: ${LINK}"
echo "        -> ${SCRIPT}"
echo "Using python3 ${PY_VERSION}"
echo

case ":${PATH}:" in
    *":${BIN}:"*)
        echo "Run:  slideshow <DIR>"
        ;;
    *)
        echo "${BIN} is not on your PATH."
        echo "Add this line to your ~/.zshrc and open a new terminal:"
        echo
        echo "    export PATH=\"${BIN}:\$PATH\""
        echo
        echo "You can also run it right now as: ${LINK} <DIR>"
        ;;
esac
