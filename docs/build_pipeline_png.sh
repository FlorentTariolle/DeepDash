#!/bin/bash
# Rebuild pipeline figure from docs/pipeline.tex.
# Outputs: static/images/pipeline.svg and static/images/pipeline.png (site),
# static/images/pipeline_live.png (README), plus architecture_pipeline.png
# for backward compatibility.
# Requires: xelatex, pdftocairo, pdftoppm, Python with Pillow.
set -e
cd "$(dirname "$0")"

xelatex -interaction=nonstopmode -halt-on-error pipeline.tex > /dev/null
xelatex -interaction=nonstopmode -halt-on-error pipeline_live.tex > /dev/null

pdftocairo -svg pipeline.pdf static/images/pipeline.svg

pdftoppm -png -r 300 pipeline.pdf pipeline_tmp
pdftoppm -png -r 300 pipeline_live.pdf pipeline_live_tmp
python -c "
from PIL import Image, ImageDraw
img = Image.open('pipeline_tmp-1.png').convert('RGBA')
w, h = img.size
img = img.crop((0, 0, w, h - 12))
w, h = img.size
mask = Image.new('L', (w, h), 0)
draw = ImageDraw.Draw(mask)
draw.rounded_rectangle([(0, 0), (w, h)], radius=30, fill=255)
img.putalpha(mask)
img.save('static/images/pipeline.png')
img.save('architecture_pipeline.png')

live = Image.open('pipeline_live_tmp-1.png').convert('RGBA')
mask = Image.new('L', live.size, 0)
draw = ImageDraw.Draw(mask)
draw.rounded_rectangle([(0, 0), live.size], radius=20, fill=255)
live.putalpha(mask)
live.save('static/images/pipeline_live.png')
"

rm -f pipeline_tmp-1.png pipeline_live_tmp-1.png \
  pipeline.pdf pipeline.aux pipeline.log \
  pipeline_live.pdf pipeline_live.aux pipeline_live.log
echo "Done: static/images/pipeline.svg, static/images/pipeline.png, static/images/pipeline_live.png, architecture_pipeline.png"
