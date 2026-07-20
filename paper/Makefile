TEX = pdflatex -interaction=nonstopmode
BIB = bibtex
SRC = main
PY = python

all: figures $(SRC).pdf

figures:
	$(PY) -c "import os; os.makedirs('figures', exist_ok=True)"
	$(PY) ../scripts/gen_sls_figure.py --output figures/sls_kernel.pdf --slide-output ../presentation/figures/sls_kernel.png
	$(PY) ../scripts/gen_continuation_figure.py --config ../configs/deepdash/v7-phase0.yaml --vae-checkpoint ../checkpoints_v7/fsq_best.pt --transformer-checkpoint ../checkpoints_v7/transformer_best.pt --episodes-dir ../data/deepdash/death_episodes --expert-episodes-dir ../data/deepdash/expert_episodes --output figures/continuation.png
	cd ../docs && $(TEX) pipeline.tex
	$(PY) -c "import shutil; shutil.copy('../docs/pipeline.pdf', 'figures/pipeline.pdf')"

$(SRC).pdf: $(SRC).tex references.bib checklist.tex neurips_2026.sty figures/pipeline.pdf figures/sls_kernel.pdf figures/continuation.png
	$(TEX) $(SRC)
	$(BIB) $(SRC)
	$(TEX) $(SRC)
	$(TEX) $(SRC)

clean:
	$(PY) -c "import os; files=['$(SRC).aux','$(SRC).bbl','$(SRC).blg','$(SRC).log','$(SRC).out','$(SRC).pdf','$(SRC).fls','$(SRC).fdb_latexmk','$(SRC).synctex.gz','figures/sls_kernel.pdf','figures/pipeline.pdf','figures/continuation.png']; [os.remove(f) for f in files if os.path.exists(f)]"

.PHONY: all clean figures
