# Daily AI News Digest

Researcher -> Drafter -> Checker -> Human -> Reporter, built with LangGraph.

## Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Create a .env file (see .env.example), then:
python main.py --test-checker     # offline planted-error tests
python main.py --no-human         # full run
uvicorn server:app --port 8000    # backend for the UI
cd digest-ui; npm install; npm run dev
