FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY ["claims-engine (1)/webapp/requirements.txt", "webapp/requirements.txt"]
RUN pip install --no-cache-dir -r webapp/requirements.txt
COPY ["claims-engine (1)/engine", "engine"]
COPY ["claims-engine (1)/eval", "eval"]
COPY ["claims-engine (1)/seed/out/lot1.json", "seed/out/lot1.json"]
COPY ["claims-engine (1)/prompts", "prompts"]
COPY ["claims-engine (1)/schemas", "schemas"]
COPY ["claims-engine (1)/webapp", "webapp"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
