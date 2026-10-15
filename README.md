# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne engineering assignment.

## Requirements
- Python 3.11+ recommended by the assignment (the local troubleshooting environment used Python 3.10 32-bit).
- MongoDB 6.0+; the department trend uses `$densify` and the leaderboard uses `$setWindowFields`.

## Setup (Windows CMD)
```cmd
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/docs`. Keep the terminal running while testing. If port 8000 is already in use, check the existing server before starting another one.

On macOS/Linux, create/activate a virtual environment with `python3 -m venv .venv`, install requirements, export `MONGO_URI` and `MONGO_DB`, then run `python -m uvicorn app.main:app --port 8000`.

## Configuration
`MONGO_URI` and `MONGO_DB` are read from environment variables. A local `.env` is supported by `python-dotenv`; environment variables take precedence. Do not commit `.env`, secrets, or `.venv`.

## Main endpoints
- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Important status
The app starts and the health/docs endpoints were observed responding locally during development. This is not a claim that every hidden-dataset, concurrency, or explain-plan requirement has passed. Run the contract tests against MongoDB 6.0+ and review `REVIEW.md` before submitting. The current explain endpoint implementation is limited and should be completed to satisfy all five required explain endpoint names and matching filters.
