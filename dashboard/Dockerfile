FROM python:3.12-slim

WORKDIR /app

# Install dashboard dependencies only — no Playwright, no Chrome
COPY dashboard/requirements.txt /tmp/requirements-dashboard.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dashboard.txt

# Copy full project so dashboard can import core/models/database
COPY . .

CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8080"]
