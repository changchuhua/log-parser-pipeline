FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y openssh-client && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract_so_logs.py .

CMD ["python", "extract_so_logs.py"]
