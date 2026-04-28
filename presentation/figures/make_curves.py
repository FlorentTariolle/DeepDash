"""Generate V3-deploy training curves for the presentation."""
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd

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
    ax.plot(df.epoch, df.lr, color=LR_C)
    ax.set_title("Learning rate")
    ax.set_xlabel("epoch")

    fig.savefig(OUT / "fsq_curves.png", bbox_inches="tight")
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
    ax.plot(df.epoch, df.lr, color=LR_C)
    ax.set_title("Learning rate")
    ax.set_xlabel("epoch")

    fig.savefig(OUT / "transformer_curves.png", bbox_inches="tight")
    plt.close(fig)


def controller_curves():
    bc = pd.read_csv(BC_LOG)
    ppo = pd.read_csv(PPO_LOG)

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
    if "eval_survival" in ppo.columns:
        ax.plot(ppo.iteration, ppo.eval_survival, color=VAL_C, label="eval survival")
    if "mean_survival" in ppo.columns:
        ax.plot(ppo.iteration, ppo.mean_survival, color=TRAIN_C, alpha=0.6, label="train survival")
    ax.set_title("PPO survival")
    ax.set_xlabel("iteration")
    ax.legend()

    ax = axes[3]
    if "lr" in ppo.columns:
        ax.plot(ppo.iteration, ppo.lr, color=LR_C, label="PPO lr")
    ax.set_title("PPO learning rate")
    ax.set_xlabel("iteration")

    fig.savefig(OUT / "controller_curves.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    fsq_curves()
    transformer_curves()
    controller_curves()
    print("Saved:", *(p.name for p in OUT.glob("*.png")))
