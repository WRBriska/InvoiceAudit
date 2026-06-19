.PHONY: install generate audit eval golden extract-demo extract-eval all clean

install:
	pip install -r requirements.txt

generate:
	python3 generate_invoices.py

audit:
	python3 run_audit.py

eval:
	python3 evaluate.py

# Independent oracle: run the real engine against a hand-labeled golden set whose
# expected dollars are literals (not the engine's own formula). Exits nonzero on drift.
golden:
	python3 evaluate_golden.py

# Show one invoice go structured -> messy text -> LLM extraction -> audit
extract-demo:
	python3 extract.py

# Measure LLM recall of the text extraction front-end
extract-eval:
	python3 evaluate_extraction.py

# Full pipeline: synthetic data -> audit -> scorecard -> independent golden check
all: generate audit eval golden

clean:
	rm -f synthetic_invoices.jsonl answer_keys.jsonl audits.jsonl eval_report.json extraction_report.json golden_report.json
