"""Render the paper-ready spike counterfactual figure.

This script does not rerun the world model. It reads the archived generated
frames and death scores produced by ``gen_counterfactual_spike.py`` and changes
only their presentation. The reduced set of time points makes each frame
substantially larger at manuscript text width.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for name in names:
        path = Path(name)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def read_frame(path: Path, tile: int) -> Image.Image:
    with Image.open(path) as source:
        return source.convert("RGB").resize((tile, tile), Image.Resampling.NEAREST)


def arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], color: str) -> None:
    draw.line([start, end], fill=color, width=7)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 22
    spread = 0.55
    points = [
        end,
        (
            int(end[0] - length * math.cos(angle - spread)),
            int(end[1] - length * math.sin(angle - spread)),
        ),
        (
            int(end[0] - length * math.cos(angle + spread)),
            int(end[1] - length * math.sin(angle + spread)),
        ),
    ]
    draw.polygon(points, fill=color)


def render(analysis_dir: Path, output: Path) -> None:
    metadata = json.loads((analysis_dir / "metadata.json").read_text(encoding="utf-8"))
    jump_probs = metadata["jump_death_probabilities"]
    idle_probs = metadata["idle_death_probabilities"]

    tile = 240
    gap = 22
    pad = 30
    shared_x = pad
    shared_y = 230
    branch_gap = 135
    grid_x = shared_x + tile + branch_gap
    top_y = 70
    bottom_y = 405
    footer_h = 60
    width = grid_x + 4 * tile + 3 * gap + pad
    height = bottom_y + tile + footer_h + 18

    bg = "#f7f8fa"
    ink = "#20242b"
    muted = "#5f6875"
    shared_color = "#59636f"
    idle_color = "#c05232"
    death_color = "#d72336"
    jump_color = "#3f7f2a"
    canvas = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(canvas)

    row_font = load_font(30, bold=True)
    label_font = load_font(23, bold=True)
    small_font = load_font(20)
    tiny_font = load_font(18)

    shared = read_frame(analysis_dir / "shared_context" / "frame_000003.png", tile)
    canvas.paste(shared, (shared_x, shared_y))
    draw.rounded_rectangle(
        [shared_x, shared_y, shared_x + tile - 1, shared_y + tile - 1],
        radius=5,
        outline=shared_color,
        width=5,
    )
    draw.text((shared_x + 22, shared_y + tile + 12), "shared context", fill=ink, font=label_font)
    draw.text((shared_x + 74, shared_y + tile + 38), "t = 174", fill=muted, font=tiny_font)

    draw.text((grid_x, top_y - 42), "IDLE", fill=idle_color, font=row_font)
    draw.text((grid_x + 96, top_y - 36), "no jump at branch point", fill=muted, font=small_font)
    draw.text((grid_x, bottom_y - 42), "JUMP", fill=jump_color, font=row_font)
    draw.text((grid_x + 112, bottom_y - 36), "jump at branch point", fill=muted, font=small_font)

    origin = (shared_x + tile + 8, shared_y + tile // 2)
    arrow(draw, origin, (grid_x - 16, top_y + tile // 2), idle_color)
    arrow(draw, origin, (grid_x - 16, bottom_y + tile // 2), jump_color)

    def place_frame(path: Path, x: int, y: int, border: str, step: int, score: float, death: bool = False) -> None:
        canvas.paste(read_frame(path, tile), (x, y))
        draw.rounded_rectangle([x, y, x + tile - 1, y + tile - 1], radius=5, outline=border, width=5)
        draw.text((x + 6, y + tile + 10), f"t+{step}", fill=ink, font=label_font)
        suffix = f"death score {score:.2f}"
        if death:
            suffix = f"DEATH  |  score {score:.2f}"
        draw.text((x + 62, y + tile + 14), suffix, fill=death_color if death else muted, font=tiny_font)

    selected = [0, 4]
    for column, index in enumerate(selected):
        x = grid_x + column * (tile + gap)
        place_frame(
            analysis_dir / "idle" / f"frame_{index:06d}.png",
            x,
            top_y,
            death_color if index == 4 else idle_color,
            index + 1,
            idle_probs[index],
            death=index == 4,
        )

    terminated_x = grid_x + 2 * (tile + gap)
    terminated_width = 2 * tile + gap
    draw.rounded_rectangle(
        [terminated_x, top_y, terminated_x + terminated_width - 1, top_y + tile - 1],
        radius=9,
        fill="#eceff3",
        outline="#b6bdc7",
        width=4,
    )
    terminated_center = terminated_x + terminated_width // 2
    draw.text(
        (terminated_center, top_y + 88),
        "rollout terminated",
        fill=death_color,
        font=row_font,
        anchor="mm",
    )
    draw.text(
        (terminated_center, top_y + 136),
        "after t+5",
        fill=muted,
        font=label_font,
        anchor="mm",
    )
    draw.text(
        (terminated_center, top_y + 180),
        "t+9 and t+13 not generated",
        fill=muted,
        font=small_font,
        anchor="mm",
    )

    jump_selected = [0, 4, 8, 12]
    for column, index in enumerate(jump_selected):
        x = grid_x + column * (tile + gap)
        place_frame(
            analysis_dir / "jump" / f"frame_{index:06d}.png",
            x,
            bottom_y,
            jump_color,
            index + 1,
            jump_probs[index],
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        default=ROOT / "analysis" / "2026-07-21_counterfactual_spike",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "paper" / "figures" / "continuation.png",
    )
    args = parser.parse_args()
    render(args.analysis_dir.resolve(), args.output.resolve())
    print(f"Saved {args.output.resolve()}")


if __name__ == "__main__":
    main()
