FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.supernova.lock ./
RUN pip install --no-cache-dir -r requirements.supernova.lock && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY supernova /app/supernova
USER 10001:10001
CMD ["python", "-m", "supernova.serve"]
