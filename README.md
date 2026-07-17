# Plex Remote

Local FastAPI endpoints for Plex HTPC, TV CEC controls, Sunshine, and a React dashboard.

## Backend

Run the API with `uv`:

```bash
uv run python main.py
```

The API continues to expose JSON endpoints such as `/status`, `/plex/play`, `/plex/stop`, `/plex/start`, `/sunshine/start`, and `/tv/power/on` for Home Assistant or other callers.

## Home Assistant Occupancy

Add the Home Assistant address and a long-lived access token to `.env`:

```bash
touch .env && chmod 600 .env
printf '%s\n' 'HOME_ASSISTANT_URL=http://homeassistant.local:8123' >> .env
read -rsp 'Home Assistant token: ' HA_TOKEN && printf '\nHOME_ASSISTANT_ACCESS_TOKEN=%s\n' "$HA_TOKEN" >> .env && unset HA_TOKEN
```

Restart the API after setting these values. The server authenticates to Home
Assistant over `/api/websocket`, caches the chair and bed binary sensors, and
automatically reconnects after a disconnect. Only those two entities are
included in the ongoing state-change subscription.

## Frontend

The SPA lives in `frontend/`.

```bash
cd frontend
npm install
npm run dev
```

The dev server proxies API calls to `http://127.0.0.1:8000`, so keep FastAPI running separately while developing.

Build the SPA for FastAPI to serve:

```bash
cd frontend
npm run build
```

After a build, FastAPI serves:

- `/` for the responsive dashboard
- `/echo` for the Echo Show 5 layout
- `/settings` for detailed service controls

## Curated Media

Set curated Plex rating keys in `.env` as JSON arrays:

```bash
CURATED_MOVIES='[{"rating_key":123},{"rating_key":456,"months":[12]},{"rating_key":789,"random":false}]'
CURATED_SHOWS='[{"rating_key":321},{"rating_key":654,"seasons":[3,4,5]}]'
```

Movie `months` is optional. If present, that movie is only included in random movie selection during those months.

Movie `random` is optional and defaults to `true`. Set `"random":false` to keep a movie visible for manual selection while excluding it from random movie selection.

Show `seasons` is optional. If omitted or empty, random selection can use any season. If present, random selection and the dashboard season browser are limited to those season numbers.

`POST /plex/play` without `media_id` picks a random active curated movie. `POST /plex/play?media_id=<show_rating_key>` picks a random episode from the configured seasons for that show. Passing a season rating key picks a random episode from that season; passing an episode rating key plays that episode directly.
