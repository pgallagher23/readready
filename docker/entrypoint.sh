#!/bin/sh
# Container start: (1) restore the TeX Gyre font links if a tmpfs hid them,
# (2) register user-supplied fonts (mounted at /app/fonts) with fontconfig,
# then run CMD. Refresh the fontconfig cache whenever (1) or (2) changed anything.
set -e
refresh=0

# The image links the TeX Gyre OTFs into /usr/local/share/fonts/texgyre at build time.
# A tmpfs over /usr/local/share/fonts (read-only compose services) hides them, so recreate.
if [ ! -d /usr/local/share/fonts/texgyre ]; then
    mkdir -p /usr/local/share/fonts/texgyre
    ln -sf /usr/share/texmf/fonts/opentype/public/tex-gyre/*.otf /usr/local/share/fonts/texgyre/ 2>/dev/null || true
    refresh=1
fi

if [ -d /app/fonts ] && [ -n "$(ls -A /app/fonts 2>/dev/null | grep -v -e README.md -e .gitkeep -e manifest.json)" ]; then
    if mkdir -p /usr/local/share/fonts 2>/dev/null \
        && ln -sfn /app/fonts /usr/local/share/fonts/readready-user 2>/dev/null; then
        refresh=1
    else
        echo "warning: could not register fonts from /app/fonts (font directory not writable); continuing without them" >&2
    fi
fi

if [ "$refresh" = "1" ]; then
    fc-cache -f >/dev/null 2>&1 || true
fi
exec "$@"
