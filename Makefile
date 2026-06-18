.PHONY: install generate audit eval all clean

install:
	pip install -r requirements.txt

generate:
	python3 generate_invoices.py

audit:
	python3 run_audit.py

eval:
	python3 evaluate.py

# Full pipeline: synthetic data -> audit -> scorecard
all: generate audit eval

clean:
	rm -f synthetic_invoices.jsonl answer_keys.jsonl audits.jsonl eval_report.json
