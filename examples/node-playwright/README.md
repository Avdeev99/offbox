# Vite and Playwright

After creating or initializing an offbox host, copy this example into its own Git repository. Offbox reads configuration at the Git worktree root, so running it directly inside this repository's `examples/` subdirectory would use the wrong root:

```sh
cp -R examples/node-playwright ../offbox-node-playwright
cd ../offbox-node-playwright
git init
git add .
git commit -m "Add offbox example"
offbox up
offbox exec -- npm test
offbox exec -- npm run build
offbox ports
```

Open the local mapping for remote port 3000 to use the counter. Vite runs in a Node 22 Compose service. Playwright runs Chromium inside the universal offbox environment and reaches that service through localhost port 3000. The test clicks the counter twice, checks its state and resets it. The test configuration uses the existing Compose service rather than starting another server.

`up` runs `npm ci && npx playwright install chromium`, then starts the service and waits for its HTTP healthcheck. The service binds the whole work directory, including environment-local `node_modules`, so its Node major matches the Node used by installation. Source changes sync through the directory bind and Vite reloads them. There is no separate stale dependency volume.

After changing dependencies, update and commit `package-lock.json`. The next `offbox exec` flushes it and reruns installation when its hash changes. A second linked worktree can use the same Compose file and remote port 3000; offbox assigns another local port when necessary.

```sh
offbox stop
offbox up
offbox rm --yes
```

The browser tests require a running stack and installed Chromium; they are not part of offbox's daemon-free unit suite.
