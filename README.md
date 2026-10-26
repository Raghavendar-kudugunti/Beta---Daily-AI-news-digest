# Daily AI News Digest

Two run modes built with LangGraph: a Day 1 single-agent baseline and the Day 2 Researcher -> Drafter -> Checker -> Human -> Reporter workflow.

## Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Create a .env file (see .env.example), then:
python main.py --test-checker     # offline planted-error tests
python main.py --single-agent     # Day 1 end-to-end single-agent workflow
python main.py --no-human         # full run
uvicorn server:app --port 8000    # backend for the UI
cd digest-ui; npm install; npm run dev
