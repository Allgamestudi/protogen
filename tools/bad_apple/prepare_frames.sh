#!/bin/bash
# Конвертирует видео в raw grayscale-поток кадров 64×32 @ 30fps
# и опционально извлекает аудио-дорожку в mp3.
#
# Usage: ./prepare_frames.sh input.mp4 [output_dir]
#
# Requires: ffmpeg

set -e

IN="${1:?Usage: $0 input.mp4 [output_dir]}"
OUTDIR="${2:-$HOME/protogen-media/bad_apple}"

mkdir -p "$OUTDIR"

echo "== Extracting frames 64×32 grayscale @ 30fps..."
ffmpeg -y -i "$IN" \
    -r 30 \
    -vf "scale=64:32:flags=lanczos,format=gray" \
    -pix_fmt gray -f rawvideo \
    "$OUTDIR/bad_apple.gray"

BYTES=$(stat -c%s "$OUTDIR/bad_apple.gray")
FRAMES=$((BYTES / 2048))
echo "== $FRAMES frames written ($BYTES bytes)"

echo "== Extracting audio..."
ffmpeg -y -i "$IN" -vn -acodec libmp3lame -q:a 5 "$OUTDIR/bad_apple.mp3"

echo "Done."
echo "Play:  sudo $HOME/protogen/tools/bad_apple/bad_apple_player \\"
echo "         $OUTDIR/bad_apple.gray $OUTDIR/bad_apple.mp3"
