.PHONY: run test lint build up down

run:
	python run.py

test:
	pytest tests/ -v

lint:
	flake8 app/ tests/

build:
	docker build -t dockd .

up:
	docker-compose up -d

down:
	docker-compose down
