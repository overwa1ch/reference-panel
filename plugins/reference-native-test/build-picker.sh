#!/bin/sh
set -eu
picker_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$picker_root/bin"
# Universal (Apple silicon + Intel) binaries for macOS 12 or later.
target="-arch arm64 -arch x86_64 -mmacosx-version-min=12.0"
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Werror $target -framework AppKit \
  "$picker_root/native-picker.m" -o "$picker_root/bin/reference-picker"
xcrun clang -fobjc-arc -O2 -Wall -Wextra -Werror $target -framework AppKit -framework AVFoundation -framework CoreMedia -framework ImageIO \
  -framework PDFKit -framework QuickLookThumbnailing \
  "$picker_root/native-render.m" -o "$picker_root/bin/reference-render"
