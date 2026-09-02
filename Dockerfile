FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements-prometheus.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # --no-deps: django-prometheus's metadata declares Django<6.1 (stale --
    # see requirements-prometheus.txt) which would conflict with the
    # Django==6.1 pin above if resolved together; prometheus-client (its
    # real dependency) is already installed normally from requirements.txt.
    && pip install --no-cache-dir --no-deps -r requirements-prometheus.txt

COPY . .

# Baked into the image at build time so it doesn't need DATA_DIR/DB access --
# admin/docs CSS+JS are static, unlike the DB which is only available once
# the PVC is mounted at container start.
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Overridden per-service in k8s (worker / beat) and in docker-compose.yml's
# dev services (which use runserver instead, for autoreload); this is the
# production default -- gunicorn, not Django's single-threaded dev server.
CMD ["gunicorn", "k8s_api.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
