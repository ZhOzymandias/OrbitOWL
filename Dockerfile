FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.orbitowl.lock ./
RUN pip install --no-cache-dir -r requirements.orbitowl.lock && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY orbitowl /app/orbitowl
RUN chmod -R a+rX /app/orbitowl
USER 10001:10001
CMD ["python", "-m", "orbitowl.serve"]
