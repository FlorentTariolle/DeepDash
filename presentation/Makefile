TEX = pdflatex
SRC = main.tex
PDF = main.pdf
CURVES = figures/fsq_curves.png figures/transformer_curves.png figures/controller_curves.png
CURVE_LOGS = ../experiments/v3_deploy/fsq_log.csv ../experiments/v3_deploy/transformer_log.csv ../experiments/v3_deploy/controller_bc_log.csv ../experiments/v3_deploy/controller_ppo_log.csv
SLS_FIG = figures/sls_kernel.png

all: $(PDF)

$(PDF): $(SRC) $(CURVES) $(SLS_FIG)
	$(TEX) $(SRC)
	$(TEX) $(SRC)

$(CURVES): figures/make_curves.py $(CURVE_LOGS)
	python figures/make_curves.py

$(SLS_FIG): ../scripts/gen_sls_figure.py
	cd .. && python scripts/gen_sls_figure.py

clean:
	rm -f *.aux *.log *.nav *.out *.snm *.toc *.vrb

.PHONY: all clean
