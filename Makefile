.PHONY: install test sim run engine web doctor clean

install:
	pip install -r requirements.txt

test:
	PYTHONPATH=. python -m pytest

sim:
	python scripts/simulate.py --ticks 120

doctor:
	python scripts/doctor.py

run:
	python run.py

engine:
	python run_engine.py

web:
	python run_web.py

clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm logs/*.log logs/*.lock
	find . -name __pycache__ -type d -exec rm -rf {} +
