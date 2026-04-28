"""Generate V3-deploy training curves for the presentation."""
from pathlib import Path
import math
import matplotlib.pyplot as plt
import pandas as pd
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
V3 = ROOT / "experiments/v3_deploy"
FSQ_LOG = V3 / "fsq_log.csv"
TFM_LOG = V3 / "transformer_log.csv"
BC_LOG = V3 / "controller_bc_log.csv"
PPO_LOG = V3 / "controller_ppo_log.csv"
OUT = Path(__file__).parent

plt.rcParams.update({
    "figure.dpi": 130,
    "savefig.dpi": 160,
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.labelsize": 10,
    "legend.fontsize": 9,
    "axes.grid": True,
    "grid.alpha": 0.3,
})

TRAIN_C = "#2E86AB"
VAL_C = "#E51F13"
LR_C = "#7FB069"
SLIDE_BG = "#FAFAFA"
WHITE_THRESHOLD = 235


def savefig_slide_bg(fig, path):
    """Save and replace white/near-white pixels with Metropolis background."""
    fig.savefig(path, bbox_inches="tight")
    image = Image.open(path).convert("RGBA")
    pixels = image.load()
    replacement = tuple(int(SLIDE_BG[i:i + 2], 16) for i in (1, 3, 5))

    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            if a > 0 and r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
                pixels[x, y] = (*replacement, a)
    image.save(path)


def cosine_lr_schedule(epochs, peak_lr=1e-3, min_lr=1e-5, total_epochs=200):
    """Reconstruct CosineAnnealingLR values; CSV LR is rounded."""
    values = []
    for epoch in epochs:
        progress = min(max(epoch / total_epochs, 0.0), 1.0)
        cos = 0.5 * (1.0 + math.cos(math.pi * progress))
        values.append(min_lr + (peak_lr - min_lr) * cos)
    return values


def transformer_lr_schedule(epochs, peak_lr=2e-3, min_lr=5e-5,
                            total_epochs=200, warmup_epochs=5):
    """Reconstruct the warmup+cosine schedule; CSV LR is rounded."""
    start_factor = 1e-2
    cosine_length = max(1, total_epochs - warmup_epochs)
    final_ratio = min_lr / peak_lr
    values = []
    for epoch in epochs:
        if epoch < warmup_epochs:
            factor = start_factor + (1.0 - start_factor) * epoch / warmup_epochs
        else:
            progress = (epoch - warmup_epochs) / cosine_length
            progress = min(max(progress, 0.0), 1.0)
            cos = 0.5 * (1.0 + math.cos(math.pi * progress))
            factor = final_ratio + (1.0 - final_ratio) * cos
        values.append(peak_lr * factor)
    return values


def fsq_curves():
    df = pd.read_csv(FSQ_LOG)
    fig, axes = plt.subplots(1, 4, figsize=(13, 2.7), constrained_layout=True)
    ax = axes[0]
    ax.plot(df.epoch, df.train_recon, color=TRAIN_C, label="train")
    ax.plot(df.epoch, df.val_recon, color=VAL_C, label="val")
    ax.set_title("Reconstruction (MSE)")
    ax.set_xlabel("epoch")
    ax.set_yscale("log")
    ax.legend()

    ax = axes[1]
    ax.plot(df.epoch, df.train_slow, color=TRAIN_C, label="train")
    ax.set_title("GRWM slowness")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[2]
    ax.plot(df.epoch, df.train_uniform, color=TRAIN_C, label="train")
    ax.set_title("GRWM uniformity")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[3]
    ax.plot(df.epoch, cosine_lr_schedule(df.epoch), color=LR_C)
    ax.set_title("Learning rate")
    ax.set_xlabel("epoch")

    savefig_slide_bg(fig, OUT / "fsq_curves.png")
    plt.close(fig)


def transformer_curves():
    df = pd.read_csv(TFM_LOG)
    fig, axes = plt.subplots(1, 5, figsize=(15.5, 2.7), constrained_layout=True)

    ax = axes[0]
    ax.plot(df.epoch, df.train_loss, color=TRAIN_C, label="train")
    ax.plot(df.epoch, df.val_loss, color=VAL_C, label="val")
    ax.set_title("CE+SLS loss")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[1]
    ax.plot(df.epoch, df.train_acc, color=TRAIN_C, label="train")
    ax.plot(df.epoch, df.val_acc, color=VAL_C, label="val")
    ax.set_title("Token accuracy")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[2]
    ax.plot(df.epoch, df.train_death_f1, color=TRAIN_C, label="train")
    ax.plot(df.epoch, df.val_death_f1, color=VAL_C, label="val")
    ax.set_title("Death F1")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[3]
    ax.plot(df.epoch, df.train_cpc, color=TRAIN_C, label="train")
    ax.plot(df.epoch, df.val_cpc, color=VAL_C, label="val")
    ax.set_title("AC-CPC loss")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[4]
    ax.plot(df.epoch, transformer_lr_schedule(df.epoch), color=LR_C)
    ax.set_title("Learning rate")
    ax.set_xlabel("epoch")

    savefig_slide_bg(fig, OUT / "transformer_curves.png")
    plt.close(fig)


def controller_curves():
    bc = pd.read_csv(BC_LOG)
    ppo = pd.read_csv(PPO_LOG)
    ppo_x = range(len(ppo))
    ppo_eval = ppo[ppo.eval_survival.notna()] if "eval_survival" in ppo.columns else None
    ppo_eval_x = ppo_eval.index if ppo_eval is not None else None

    fig, axes = plt.subplots(1, 4, figsize=(13, 2.7), constrained_layout=True)

    ax = axes[0]
    ax.plot(bc.epoch, bc.train_loss, color=TRAIN_C, label="train")
    ax.plot(bc.epoch, bc.val_loss, color=VAL_C, label="val")
    ax.set_title("BC loss")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[1]
    ax.plot(bc.epoch, bc.train_acc, color=TRAIN_C, label="train")
    ax.plot(bc.epoch, bc.val_acc, color=VAL_C, label="val")
    ax.set_title("BC accuracy")
    ax.set_xlabel("epoch")
    ax.legend()

    ax = axes[2]
    if "mean_survival" in ppo.columns:
        train_survival = pd.to_numeric(ppo.mean_survival, errors="coerce")
        train_ema = train_survival.ewm(alpha=1 - 0.99, adjust=False).mean()
        ax.plot(ppo_x, train_ema, color=TRAIN_C, alpha=0.85, label="train EMA 0.99")
    if ppo_eval is not None:
        eval_survival = pd.to_numeric(ppo_eval.eval_survival, errors="coerce")
        eval_ema = eval_survival.ewm(alpha=1 - 0.99, adjust=False).mean()
        ax.plot(ppo_eval_x, eval_ema, color=VAL_C, label="eval EMA 0.99")
    ax.set_title("PPO survival")
    ax.set_xlabel("iteration")
    ax.legend()

    ax = axes[3]
    if "lr" in ppo.columns:
        ax.plot(ppo_x, ppo.lr, color=LR_C, label="PPO lr")
    ax.set_title("PPO learning rate")
    ax.set_xlabel("iteration")

    savefig_slide_bg(fig, OUT / "controller_curves.png")
    plt.close(fig)


if __name__ == "__main__":
    fsq_curves()
    transformer_curves()
    controller_curves()
    print("Saved:", *(p.name for p in OUT.glob("*.png")))
