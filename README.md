# Kubernetes App Platform

A small self-service platform that sits on top of Kubernetes and lets an authenticated user (or an admin) create clusters, namespaces, and apps **without writing or applying any YAML by hand**. You register a cluster's kubeconfig once, then use a web UI (or a plain REST API) to spin up a namespace and, inside it, a Deployment + Service pair for something like `nginx` — the backend translates that click into the actual Kubernetes API calls.

**Live:** [golahmar.osdl.ir](https://golahmar.osdl.ir)

It ships as two independently deployable, separately-versioned repos that together form one product behind a single domain/Ingress:

| Project | Repo | What it is |
|---|---|---|
| **Backend** | [`k8s-api`](https://github.com/AmirmahdiGolahmar/k8s-api) (this repository) | Django REST API that talks to the Kubernetes API on the user's behalf, plus Celery workers for backups |
| **Frontend** | [`k8s-api-frontend`](https://github.com/AmirmahdiGolahmar/k8s-api-frontend) | React SPA (dashboard) that consumes the backend API |

---

## 1. The core idea: Kubernetes as a product, not a YAML exercise

The design goal is to make "give me an nginx in a namespace" a two-click operation instead of a `kubectl apply -f`:

1. An admin registers a **Cluster** — either the kubeconfig text is stored in the database, or the field is left blank and the backend falls back to the local/in-cluster default kubeconfig (see [`clusters/k8s_client.py`](clusters/k8s_client.py)).
2. A user picks a cluster and creates a **Namespace** through the API — the backend calls the real Kubernetes API and only writes a DB row once the namespace is confirmed to exist.
3. Inside that namespace, the user creates an **App** by picking an image from a small catalog ([`appCatalog.js`](https://github.com/AmirmahdiGolahmar/k8s-api-frontend/blob/main/src/data/appCatalog.js) in the frontend repo) — today only `nginx:latest` is wired up end-to-end, with Rocket.Chat, GitLab Runner, Keycloak, Nextcloud, Jupyter, Grafana, etc. present in the catalog UI as placeholders for the same pattern. Creating an App provisions a Kubernetes **Deployment** + **Service** pair through the [`kubernetes` Python client](https://github.com/kubernetes-client/python).
4. Every object the platform manages (Cluster, Namespace, App) has a corresponding row in Postgres/SQLite that mirrors Kubernetes state — Kubernetes is the source of truth for whether something *runs*, the DB is the source of truth for who *owns* it and whether the platform is allowed to touch it.

Because clusters, namespaces and apps are modeled as first-class resources with ownership and access control (see §4), the same mechanism generalizes to "any short-lived workload someone wants to try out inside a namespace they control" — nginx is simply the first fully wired example.

On top of that, the platform can also **back up files out of a running app's pod** on demand or on a cron schedule (the `backups` app), and it ships its own **observability stack** (Prometheus/VictoriaMetrics + Grafana) so operators can see the platform's own health, not just the workloads it creates.

---

## 2. Repository layout

### 2.1 Backend —  [`k8s-api`](https://github.com/AmirmahdiGolahmar/k8s-api)

```
k8s-api/
├── accounts/                 # session-based auth: register/login/logout/me
├── clusters/                 # Cluster / Namespace / App models, views, k8s client
├── backups/                  # Backup / BackupSchedule models, Celery tasks
├── k8s_api/                  # Django settings, urls, celery app
├── manifests/                # Kubernetes manifests for the backend + Redis
├── monitoring/                # Prometheus/VictoriaMetrics manifests 
├── docker-compose.yml        # Full local dev stack (web, worker, beat, redis, prometheus, grafana)
├── manage.py
├── requirements.txt
└── Dockerfile
```

### 2.2 Frontend — [`k8s-api-frontend`](https://github.com/AmirmahdiGolahmar/k8s-api-frontend) (separate repo)

```
frontend/
├── src/
│   ├── pages/                 # Login, Register, Clusters, Namespaces, Apps, Backups
│   ├── components/            # AppLayout, RequireAuth, AccessModal, ImageCatalogPicker
│   ├── auth/AuthContext.jsx   # session/CSRF auth against the backend
│   ├── api/client.js          # fetch wrapper for the backend API
│   └── data/appCatalog.js     # image catalog shown in the App creation picker
├── public/
├── manifests/00-frontend.yaml
├── nginx.conf                 # SPA fallback routing for react-router
├── package.json
└── Dockerfile                 # multi-stage build → static files served by nginx
```

---

## 3. Tech stack

### 3.1 Backend —  [`k8s-api`](https://github.com/AmirmahdiGolahmar/k8s-api)

- **Language/framework:** Python 3.13, Django 6.1, Django REST Framework 3.18
- **Kubernetes access:** the official [`kubernetes`](https://pypi.org/project/kubernetes/) Python client, wrapped in [`clusters/k8s_client.py`](clusters/k8s_client.py); supports both a stored per-`Cluster` kubeconfig and the local/in-cluster default
- **Auth:** Django session authentication + CSRF (no token/JWT layer) — see [`accounts/`](accounts/); every endpoint requires `IsAuthenticated` by default (`REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']` in [`settings.py`](k8s_api/settings.py)), with `csrf`/`login`/`register` explicitly opted back out
- **Async jobs:** Celery 5 + Redis, for pod backups (`backups/tasks.py`) and scheduled cron-style backups (`BackupSchedule` + Celery Beat)
- **Persistence:** Django ORM — SQLite by default (see `db.sqlite3`, PVC-backed in the cluster), works with any Django-supported DB
- **Serving:** `gunicorn` in production (Dockerfile default `CMD`), `whitenoise` for static files, `runserver` for local dev via docker-compose
- **Observability:** `django-prometheus` metrics exposed at `/metrics`, scraped by an in-cluster VictoriaMetrics/Prometheus stack (`monitoring/`), Celery-side backup duration metrics via a Prometheus multiprocess directory
- **CI/CD:** GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml)) builds and pushes the image to `ghcr.io/amirmahdigolahmar/k8s-api` on every push to `main`

### 3.2 Frontend — [`k8s-api-frontend`](https://github.com/AmirmahdiGolahmar/k8s-api-frontend)

- **Framework:** React 19 + Vite 8
- **UI kit:** Ant Design 6 (`antd`), `@ant-design/icons`, `simple-icons` for the app-catalog logos
- **Routing:** `react-router-dom` 7 (`BrowserRouter`, real paths — see the SPA fallback in `nginx.conf`)
- **Auth:** `src/auth/AuthContext.jsx` talks to this backend's session/CSRF endpoints (`/auth/csrf/`, `/auth/login/`, …)
- **Pages:** Login, Register, Clusters, Namespaces, Apps (with the image catalog picker), Backups
- **Build/serve:** multi-stage Dockerfile — `npm run build` produces static assets, served by a minimal `nginx:alpine` image
- **Lint:** `oxlint`
- **CI/CD:** GitHub Actions builds and pushes to `ghcr.io/amirmahdigolahmar/k8s-api-frontend`

### 3.3 Infrastructure / Ops

- **Kubernetes manifests:** [`manifests/`](manifests/) in this repo (namespace, PVC, Redis, web, worker, beat, ingress, redis-exporter) and `manifests/00-frontend.yaml` in the frontend repo — backend and frontend deploy into the **same namespace** and are fronted by **one Ingress/domain** (Traefik), split into two `Ingress` objects only to work around Traefik's router-priority quirk (see the comment at the top of `manifests/06-ingress.yaml`)
- **Ingress controller:** Traefik, path-routed (`/cluster`, `/app`, `/namespace`, `/backup`, `/auth`, `/admin`, `/docs`, `/healthz`, `/static/` → backend; `/` → frontend)
- **Monitoring:** VictoriaMetrics + Grafana in-cluster (`monitoring/victoria-values.yaml`, `monitoring/10-vmservicescrapes.yaml`), or a local Prometheus + Grafana pair via docker-compose for development
- **Local dev stack:** `docker-compose.yml` in this repo brings up Redis, a one-shot `migrate` job, `web`, Celery `worker` + `beat`, `redis-exporter`, `prometheus`, and `grafana` together
- **Registry:** GitHub Container Registry (`ghcr.io`), pulled via `ghcr-pull-secret` in-cluster

---

## 4. Domain model

| Model | App | Purpose |
|---|---|---|
| `Cluster` | `clusters` | A registered Kubernetes cluster (stored kubeconfig or "use the default"); access can be restricted to specific users via `allowed_users` |
| `Namespace` | `clusters` | A namespace this backend created and tracks; has an `owner`, an `is_accessible` admin kill-switch, and a `status` (`active`/`deleting`) used to serialize concurrent deletes |
| `App` | `clusters` | A tracked Deployment+Service pair inside a namespace (image, replica count, status incl. `missing` if it disappears from Kubernetes outside the platform) |
| `Backup` | `backups` | One backup run: a file/directory copied out of a running app's pod by a Celery worker |
| `BackupSchedule` | `backups` | A cron expression that fires `Backup` runs automatically via Celery Beat |

Ownership and access are deliberately layered: **staff** always has full access; a regular user only sees/manages clusters, namespaces, and apps they own or have been explicitly granted access to via `allowed_users`.
