# logstash-dlq-decode: decodes Logstash's binary dead-letter-queue segment
# format. Pinned to a specific commit (no tagged releases exist) and reviewed
# for safety before being vendored here — see usage.md Component 2 section.
FROM golang:1.22-alpine AS dlq-decoder-build
RUN apk add --no-cache git
RUN go install github.com/saj/logstash-dlq-decode@078993e24f4a3abb25db3a2348ea798b46834fd6

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y openssh-client && rm -rf /var/lib/apt/lists/*

COPY --from=dlq-decoder-build /go/bin/logstash-dlq-decode /usr/local/bin/logstash-dlq-decode

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extract_so_logs.py .

CMD ["python", "extract_so_logs.py"]
