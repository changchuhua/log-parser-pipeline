FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY transform_to_ecs.py .

CMD ["python", "transform_to_ecs.py"]
