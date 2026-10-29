.PHONY: test restore verify verify-v1 demo oracle workbench build evidence package
PYTHON ?= python3
test:
	$(PYTHON) -m pytest -q
restore:
	$(PYTHON) scripts/restore_evidence.py
verify: restore
	PYTHONPATH=src $(PYTHON) scripts/verify_release.py
verify-v1:
	PYTHONPATH=src $(PYTHON) scripts/verify_release.py --v1-only
demo:
	PYTHONPATH=src $(PYTHON) -m evalanchor demo --output runs/demo
build:
	$(PYTHON) -m build
oracle:
	PYTHONPATH=src $(PYTHON) -m evalanchor arena oracle
workbench: restore
	PYTHONPATH=src $(PYTHON) -m evalanchor arena serve --workspace experiments
evidence:
	$(PYTHON) scripts/build_evidence_bundle.py
package: verify
	$(PYTHON) scripts/package_release.py
