FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Baked into the image at build time so it doesn't need DATA_DIR/DB access --
# admin/docs CSS+JS are static, unlike the DB which is only available once
# the PVC is mounted at container start.
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Overridden per-service in docker-compose.yml (worker / beat); this is the
# default for standalone `docker run`.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
