# FastAPI and Postgres

After creating or initializing an offbox host, copy this example into its own Git repository. Offbox reads configuration at the Git worktree root, so running it directly inside this repository's `examples/` subdirectory would use the wrong root:

```sh
cp -R examples/python-compose ../offbox-python-compose
cd ../offbox-python-compose
git init
git add .
git commit -m "Add offbox example"
offbox up
offbox exec -- uv run pytest
offbox ports
```

The API listens on remote port 8000 and Postgres on 5432. `offbox ports` prints their actual local mappings; another worktree can use different local ports. Open `/docs` on the API's forwarded port to create items. Source changes under `src/` sync into the API container and trigger Uvicorn reload. The database credentials in `compose.yaml` are for this disposable example.

`up` runs `uv sync --frozen`, builds the API and waits for both services to become healthy. Tests call the published API and query Postgres directly; each item test removes its own row. They run inside the offbox environment, where the published ports are localhost ports. The API uses its own image-local virtualenv; the environment's `.venv` is not mounted into a different Python image.

After changing dependencies, update and commit `uv.lock`. The next `offbox exec` flushes it and reruns installation when its hash changes. API image dependencies require `offbox up` to rebuild the service. `offbox up --fresh` replaces all environment Docker data, including this example's database volume.

```sh
offbox stop       # retain the environment and database
offbox up         # resume
offbox rm --yes   # remove this worktree's environment and database
```

Postgres/API tests require the running stack; they are not part of offbox's daemon-free unit suite.
