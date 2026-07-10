FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The application code will be copied
COPY . .

# Set env to look for session in /app/data
ENV SESSION_DIR=/app/data

EXPOSE 44321

# Start uvicorn
CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "44321"]
