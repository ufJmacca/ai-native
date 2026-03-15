# Run Registry UI

Standalone React/Vite operator console for inspecting `ai-native` runs published to the run registry backend.

## Local development

```bash
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:3000` and expects the run registry API to be reachable from the browser. Enter the API base URL and bearer token on the gate screen when the app loads.

## Container image

Build and run the static UI container directly:

```bash
docker build -t run-registry-ui services/run_registry_ui
docker run --rm -p 3000:80 run-registry-ui
```

The combined local stack is also available via:

```bash
docker compose -f services/run_registry/docker-compose.yml up --build
```

## Available scripts

- `npm run dev`
- `npm run build`
- `npm run test`
- `npm run preview`
