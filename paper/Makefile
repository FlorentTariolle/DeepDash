TEX = pdflatex -interaction=nonstopmode -halt-on-error
BIB = bibtex
PREPRINT = dashvmc-preprint
SUBMISSION = dashvmc-submission
PY = python

all: preprint

preprint: $(PREPRINT).pdf

submission: $(SUBMISSION).pdf

website: $(PREPRINT).pdf
	$(PY) -c "import shutil; shutil.copyfile('$(PREPRINT).pdf', '../docs/static/pdfs/dashvmc.pdf')"

figures/pipeline.pdf: ../docs/pipeline.tex
	$(PY) -c "import os; os.makedirs('figures', exist_ok=True)"
	cd ../docs && $(TEX) pipeline.tex
	$(PY) -c "import shutil; shutil.copy('../docs/pipeline.pdf', 'figures/pipeline.pdf')"

CONTINUATION_FRAMES = \
	../analysis/2026-07-21_counterfactual_spike/shared_context/frame_000003.png \
	../analysis/2026-07-21_counterfactual_spike/idle/frame_000000.png \
	../analysis/2026-07-21_counterfactual_spike/idle/frame_000004.png \
	../analysis/2026-07-21_counterfactual_spike/jump/frame_000000.png \
	../analysis/2026-07-21_counterfactual_spike/jump/frame_000004.png \
	../analysis/2026-07-21_counterfactual_spike/jump/frame_000008.png \
	../analysis/2026-07-21_counterfactual_spike/jump/frame_000012.png

figures/continuation.pdf: ../docs/continuation.tex $(CONTINUATION_FRAMES)
	$(PY) -c "import os; os.makedirs('figures', exist_ok=True)"
	cd ../docs && $(TEX) continuation.tex
	$(PY) -c "import shutil; shutil.copy('../docs/continuation.pdf', 'figures/continuation.pdf')"

$(PREPRINT).pdf: $(PREPRINT).tex main.tex references.bib neurips_2026.sty figures/pipeline.pdf figures/continuation.pdf
	$(TEX) $(PREPRINT)
	$(BIB) $(PREPRINT)
	$(TEX) $(PREPRINT)
	$(TEX) $(PREPRINT)

$(SUBMISSION).pdf: $(SUBMISSION).tex main.tex references.bib checklist.tex neurips_2026.sty figures/pipeline.pdf figures/continuation.pdf
	$(TEX) $(SUBMISSION)
	$(BIB) $(SUBMISSION)
	$(TEX) $(SUBMISSION)
	$(TEX) $(SUBMISSION)

clean:
	$(PY) -c "import os; bases=['$(PREPRINT)','$(SUBMISSION)','main']; suffixes=['.aux','.bbl','.blg','.log','.out','.pdf','.fls','.fdb_latexmk','.synctex.gz']; files=[base + suffix for base in bases for suffix in suffixes] + ['figures/pipeline.pdf','figures/continuation.pdf']; [os.remove(f) for f in files if os.path.exists(f)]"

.PHONY: all preprint submission website clean
