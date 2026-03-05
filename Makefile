.PHONY: install dev migrate seed reset-db

install:
	pip install -r requirements.txt

dev:
	uvicorn src.bidlens.main:app --reload

migrate:
	alembic upgrade head

seed:
	python seed.py

reset-db:
	rm -f bidlens.db
	alembic upgrade head
	python seed.py