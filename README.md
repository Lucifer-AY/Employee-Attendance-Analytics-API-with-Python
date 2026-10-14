# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for managing employee attendance, punch-in/out records, manual regularizations, and reporting analytics.

## Tech Stack
- **Python 3.11+**
- **FastAPI**
- **PyMongo (MongoDB 6.0 / 7.0)**
- **Uvicorn**
- **Pydantic v2**

## Local Setup & Run

### 1. Environment & Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. MongoDB
Ensure a MongoDB 6.0+ (or 7.0) instance is running locally or via Docker:
```bash
docker run -d --name mongo7 -p 27017:27017 mongo:7
```

Configure connection settings in `.env` (or via environment variables):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Start Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API docs are available at `http://localhost:8000/docs`.

### 5. Running Automated Tests
```bash
python -m pytest -v
```

## Key Files
- `app/main.py`: Complete application code containing all models, business rule helpers (R1 - R8), and endpoints.
- `REVIEW.md`: Comprehensive review of starter defects, test symptoms, and fixes (Part A).
- `DECISIONS.md`: Architectural decisions on indexes, concurrency, ranking ties, headcount, and scalability (Part C).
- `COMPREHENSIVE_TASK_REPORT.md`: In-depth documentation of what was asked, what was implemented, and how it was implemented.
