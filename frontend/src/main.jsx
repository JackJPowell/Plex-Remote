import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowLeft,
  Clapperboard,
  Home,
  Monitor,
  Network,
  Pause,
  Play,
  Power,
  RefreshCw,
  Settings,
  SkipForward,
  Square,
  Tv,
  Volume1,
  Volume2
} from "lucide-react";
import "./styles.css";

const EMPTY_NOW_PLAYING = {
  playing: false,
  state: null,
  display_title: null,
  artwork_url: null,
  progress_percent: null
};

const STATUS_HOLD_MS = 18000;
const PLAY_START_HOLD_MS = 9000;
const COMMAND_UNLOCK_MS = 650;
const STATUS_HOLDS_KEY = "plexRemote.statusHolds";

function readStoredStatusHolds() {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STATUS_HOLDS_KEY) || "{}");
    const now = Date.now();
    return Object.fromEntries(
      Object.entries(parsed).filter(([, hold]) => hold?.expiresAt > now)
    );
  } catch {
    return {};
  }
}

function writeStoredStatusHolds(holds) {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(holds).length === 0) {
      window.localStorage.removeItem(STATUS_HOLDS_KEY);
    } else {
      window.localStorage.setItem(STATUS_HOLDS_KEY, JSON.stringify(holds));
    }
  } catch {
    // Storage is a convenience; the in-memory hold still handles this session.
  }
}

function statusFromHolds(holds) {
  return Object.fromEntries(
    Object.entries(holds).map(([key, hold]) => [key, hold.value])
  );
}

async function getJson(url, signal) {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function postJson(url) {
  const response = await fetch(url, { method: "POST" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

function currentRoute() {
  const path = window.location.pathname;
  if (path === "/echo") return "echo";
  if (path === "/settings") return "settings";
  return "home";
}

function statusLabel(value, yes = "On", no = "Off") {
  if (value === true) return yes;
  if (value === false) return no;
  if (value === "on") return "On";
  if (value === "off") return "Off";
  if (value === null || value === undefined) return "Unknown";
  return String(value);
}

function statusToneClass(value) {
  if (value === true || value === "on") return "status-good";
  if (value === false || value === "off") return "status-bad";
  return "";
}

function titleFor(nowPlaying) {
  return nowPlaying?.display_title || (nowPlaying?.state ? "Starting..." : "Nothing playing");
}

function IconButton({ icon: Icon, label, onClick, href, disabled, active, tone = "default" }) {
  const className = `icon-button ${active ? "active" : ""} ${tone}`;
  if (href) {
    return (
      <a className={className} href={href} title={label} aria-label={label}>
        <Icon size={19} />
      </a>
    );
  }
  return (
    <button className={className} type="button" onClick={onClick} disabled={disabled} title={label} aria-label={label}>
      <Icon size={19} />
    </button>
  );
}

function usePlexRemote() {
  const storedStatusHolds = useMemo(() => readStoredStatusHolds(), []);
  const [status, setStatus] = useState(() => {
    const optimisticStatus = statusFromHolds(storedStatusHolds);
    return Object.keys(optimisticStatus).length > 0 ? optimisticStatus : null;
  });
  const [nowPlaying, setNowPlaying] = useState(EMPTY_NOW_PLAYING);
  const [movies, setMovies] = useState([]);
  const [shows, setShows] = useState([]);
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(new Set());
  const [lastUpdated, setLastUpdated] = useState(null);
  const statusHolds = useRef(storedStatusHolds);
  const nowPlayingHold = useRef(null);

  const mergeStatusHolds = useCallback((data) => {
    const now = Date.now();
    const active = {};
    Object.entries(statusHolds.current).forEach(([key, hold]) => {
      if (hold.expiresAt > now) {
        active[key] = hold.value;
      } else {
        delete statusHolds.current[key];
      }
    });
    writeStoredStatusHolds(statusHolds.current);
    return { ...data, ...active };
  }, []);

  const applyNowPlaying = useCallback((data) => {
    const hold = nowPlayingHold.current;
    const now = Date.now();
    if (!hold || hold.expiresAt <= now) {
      nowPlayingHold.current = null;
      setNowPlaying(data);
      return;
    }

    if (data?.playing || data?.state || data?.display_title) {
      nowPlayingHold.current = null;
      setNowPlaying(data);
      return;
    }

    setNowPlaying((old) => ({ ...old, ...hold.value }));
  }, []);

  const holdStatus = useCallback((patch, ms = STATUS_HOLD_MS) => {
    const expiresAt = Date.now() + ms;
    Object.entries(patch).forEach(([key, value]) => {
      statusHolds.current[key] = { value, expiresAt };
    });
    writeStoredStatusHolds(statusHolds.current);
    setStatus((old) => ({ ...(old || {}), ...patch }));
  }, []);

  const holdNowPlaying = useCallback((patch, ms = PLAY_START_HOLD_MS) => {
    nowPlayingHold.current = { value: patch, expiresAt: Date.now() + ms };
    setNowPlaying((old) => ({ ...old, ...patch }));
  }, []);

  const refreshStatus = useCallback(async (signal) => {
    const data = await getJson("/status", signal);
    setStatus(mergeStatusHolds(data));
    setLastUpdated(new Date());
  }, [mergeStatusHolds]);

  const refreshNowPlaying = useCallback(async (signal) => {
    const data = await getJson("/plex/now-playing", signal);
    applyNowPlaying(data);
    setLastUpdated(new Date());
  }, [applyNowPlaying]);

  const refreshCatalog = useCallback(async (signal) => {
    const [movieData, showData] = await Promise.all([
      getJson("/plex/media/movies", signal),
      getJson("/plex/media/shows", signal)
    ]);
    setMovies(movieData);
    setShows(showData);
  }, []);

  const refreshAll = useCallback(async () => {
    const controller = new AbortController();
    try {
      await Promise.all([
        refreshStatus(controller.signal),
        refreshNowPlaying(controller.signal),
        refreshCatalog(controller.signal)
      ]);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
    return () => controller.abort();
  }, [refreshCatalog, refreshNowPlaying, refreshStatus]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      refreshStatus(controller.signal),
      refreshNowPlaying(controller.signal),
      refreshCatalog(controller.signal)
    ]).catch((err) => setError(err.message));
    return () => controller.abort();
  }, [refreshCatalog, refreshNowPlaying, refreshStatus]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const controller = new AbortController();
      Promise.all([
        refreshStatus(controller.signal),
        refreshNowPlaying(controller.signal)
      ]).catch((err) => setError(err.message));
    }, 3500);
    return () => window.clearInterval(id);
  }, [refreshNowPlaying, refreshStatus]);

  const command = useCallback(async (key, url, optimistic) => {
    setPending((old) => new Set(old).add(key));
    setError(null);
    if (optimistic) optimistic({ setStatus, setNowPlaying, holdStatus, holdNowPlaying });
    const unlock = window.setTimeout(() => {
      setPending((old) => {
        const next = new Set(old);
        next.delete(key);
        return next;
      });
    }, COMMAND_UNLOCK_MS);
    try {
      await postJson(url);
      window.setTimeout(() => {
        const controller = new AbortController();
        Promise.all([
          refreshStatus(controller.signal),
          refreshNowPlaying(controller.signal)
        ]).catch((err) => setError(err.message));
      }, 550);
    } catch (err) {
      setError(err.message);
      const controller = new AbortController();
      Promise.all([
        refreshStatus(controller.signal),
        refreshNowPlaying(controller.signal)
      ]).catch(() => {});
    } finally {
      window.clearTimeout(unlock);
      setPending((old) => {
        const next = new Set(old);
        next.delete(key);
        return next;
      });
    }
  }, [holdNowPlaying, holdStatus, refreshNowPlaying, refreshStatus]);

  const actions = useMemo(() => ({
    pause: () => command("pause", "/plex/pause", ({ setNowPlaying }) => {
      setNowPlaying((old) => ({ ...old, state: "paused", playing: true }));
    }),
    resume: () => command("resume", "/plex/resume", ({ setNowPlaying }) => {
      setNowPlaying((old) => ({ ...old, state: "playing", playing: true }));
    }),
    stop: () => command("stop", "/plex/stop", ({ setNowPlaying }) => setNowPlaying(EMPTY_NOW_PLAYING)),
    next: () => command("next", "/plex/play", ({ holdNowPlaying, holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
      holdNowPlaying({ playing: true, state: "starting", display_title: "Starting..." });
    }),
    randomMovie: () => command("random-movie", "/plex/play", ({ holdNowPlaying, holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
      holdNowPlaying({ playing: true, state: "starting", display_title: "Starting movie..." });
    }),
    randomShow: (shows) => {
      const choices = shows.filter((item) => !item.unavailable);
      const item = choices[Math.floor(Math.random() * choices.length)];
      if (item) {
        command("random-show", `/plex/play?media_id=${item.rating_key}`, ({ holdNowPlaying, holdStatus }) => {
          holdStatus({ plex_htpc_running: true });
          holdNowPlaying({ playing: true, state: "starting", display_title: `${item.title}: random episode`, artwork_url: item.artwork_url });
        });
      }
    },
    playMedia: (item) => command(`media-${item.rating_key}`, `/plex/play?media_id=${item.rating_key}`, ({ holdNowPlaying, holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
      holdNowPlaying({ playing: true, state: "starting", display_title: item.type === "show" ? `${item.title}: random episode` : item.title, artwork_url: item.artwork_url });
    }),
    plexStart: () => command("plex-start", "/plex/start", ({ holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
    }),
    plexTerminate: () => command("plex-terminate", "/plex/terminate", ({ holdStatus, setNowPlaying }) => {
      holdStatus({ plex_htpc_running: false });
      nowPlayingHold.current = null;
      setNowPlaying(EMPTY_NOW_PLAYING);
    }),
    sunshineStart: () => command("sunshine-start", "/sunshine/start", ({ holdStatus }) => {
      holdStatus({ sunshine_running: true });
    }),
    sunshineStop: () => command("sunshine-stop", "/sunshine/stop", ({ holdStatus }) => {
      holdStatus({ sunshine_running: false });
    }),
    tailscaleStart: () => command("tailscale-start", "/tailscale/start", ({ holdStatus }) => {
      holdStatus({ tailscale_connected: true });
    }),
    tailscaleStop: () => command("tailscale-stop", "/tailscale/stop", ({ holdStatus }) => {
      holdStatus({ tailscale_connected: false });
    }),
    tvOn: () => command("tv-on", "/tv/power/on", ({ holdStatus }) => {
      holdStatus({ tv_status: "on" });
    }),
    tvOff: () => command("tv-off", "/tv/power/off", ({ holdStatus }) => {
      holdStatus({ tv_status: "off" });
    }),
    hdmi3: () => command("hdmi3", "/tv/source/3", ({ holdStatus }) => {
      holdStatus({ tv_source: 3 });
    }),
    volumeUp: () => command("volume-up", "/tv/volume/up", ({ setStatus }) => {
      setStatus((old) => ({ ...(old || {}), tv_volume: old?.tv_volume == null ? null : Math.min(100, old.tv_volume + 5) }));
    }),
    volumeDown: () => command("volume-down", "/tv/volume/down", ({ setStatus }) => {
      setStatus((old) => ({ ...(old || {}), tv_volume: old?.tv_volume == null ? null : Math.max(0, old.tv_volume - 5) }));
    })
  }), [command]);

  return {
    status,
    nowPlaying,
    movies,
    shows,
    error,
    pending,
    lastUpdated,
    actions,
    refreshAll
  };
}

function NowPlayingPanel({ nowPlaying, actions, pending, compact = false }) {
  const state = String(nowPlaying?.state || "").toLowerCase();
  const hasMedia = Boolean(nowPlaying?.state || nowPlaying?.display_title);
  const isPlaying = state === "playing" || state === "buffering";
  const progress = Number.isFinite(Number(nowPlaying?.progress_percent))
    ? Math.max(0, Math.min(100, Number(nowPlaying.progress_percent)))
    : 0;
  const playPause = isPlaying
    ? { icon: Pause, label: "Pause", action: actions.pause, key: "pause" }
    : state === "paused"
      ? { icon: Play, label: "Play", action: actions.resume, key: "resume" }
      : { icon: Play, label: "Play", action: actions.randomMovie, key: "random-movie" };

  return (
    <section className={`now-panel ${compact ? "compact" : ""}`}>
      <div className="control-rail">
        <IconButton icon={playPause.icon} label={playPause.label} onClick={playPause.action} disabled={pending.has(playPause.key)} active />
        <IconButton icon={Square} label="Stop" onClick={actions.stop} disabled={pending.has("stop")} />
        <IconButton icon={SkipForward} label="Next" onClick={actions.next} disabled={pending.has("next")} />
        {compact && <IconButton icon={Settings} label="Settings" href="/settings" />}
      </div>
      <div className="now-content">
        <div className="poster-wrap">
          <button className="poster-button" type="button" onClick={playPause.action} disabled={pending.has(playPause.key)} aria-label={playPause.label}>
            {nowPlaying?.artwork_url ? (
              <img className="poster" src={nowPlaying.artwork_url} alt="" />
            ) : (
              <div className="poster empty"><Clapperboard size={42} /></div>
            )}
          </button>
        </div>
        <div className="media-copy">
          <div className="eyebrow">Now Playing</div>
          <h1>{titleFor(nowPlaying)}</h1>
          <div className="state-line">{hasMedia ? statusLabel(nowPlaying.state, "Playing", "Paused") : "Idle"}</div>
          <div className="progress-track" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </div>
        </div>
      </div>
    </section>
  );
}

function MediaBrowser({ movies, shows, actions, pending, echo = false }) {
  const [mode, setMode] = useState("top");
  const [selectedShow, setSelectedShow] = useState(null);
  const [selectedSeason, setSelectedSeason] = useState(null);
  const [seasons, setSeasons] = useState([]);
  const [episodes, setEpisodes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [browserError, setBrowserError] = useState(null);
  const title = mode === "movies"
    ? "Movies"
    : mode === "seasons"
      ? selectedShow?.title || "Seasons"
      : mode === "episodes"
        ? selectedSeason?.title || "Episodes"
        : "TV Shows";

  const openShows = () => {
    setSelectedShow(null);
    setSelectedSeason(null);
    setSeasons([]);
    setEpisodes([]);
    setMode("shows");
  };

  const goBack = () => {
    if (mode === "episodes") {
      setSelectedSeason(null);
      setEpisodes([]);
      setMode("seasons");
      return;
    }
    if (mode === "seasons") {
      setSelectedShow(null);
      setSeasons([]);
      setMode("shows");
      return;
    }
    setMode("top");
  };

  const openSeasons = async (show) => {
    setSelectedShow(show);
    setSelectedSeason(null);
    setEpisodes([]);
    setLoading(true);
    setBrowserError(null);
    try {
      const data = await getJson(`/plex/media/shows/${show.rating_key}/seasons`);
      setSeasons(data);
      setMode("seasons");
    } catch (err) {
      setBrowserError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const openEpisodes = async (season) => {
    setSelectedSeason(season);
    setLoading(true);
    setBrowserError(null);
    try {
      const data = await getJson(`/plex/media/shows/${selectedShow.rating_key}/seasons/${season.rating_key}/episodes`);
      setEpisodes(data);
      setMode("episodes");
    } catch (err) {
      setBrowserError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (mode !== "top") {
    const activeItems = mode === "movies"
      ? movies
      : mode === "shows"
        ? shows
        : mode === "seasons"
          ? seasons
          : episodes;

    return (
      <section className={`browser ${echo ? "echo" : ""}`}>
        <div className="browser-head">
          <IconButton icon={ArrowLeft} label="Back" onClick={goBack} />
          <h2>{title}</h2>
        </div>
        <div className="media-list">
          {loading ? (
            <div className="empty-list">Loading</div>
          ) : browserError ? (
            <div className="empty-list">{browserError}</div>
          ) : activeItems.length === 0 ? (
            <div className="empty-list">No items configured</div>
          ) : activeItems.map((item) => {
            const pendingKey = `media-${item.rating_key}`;
            const isDisabled = item.unavailable || pending.has(pendingKey);
            const subtitle = item.unavailable
              ? "Unavailable"
              : mode === "movies" && item.months?.length
                ? `Month ${item.months.join(", ")}`
                : mode === "episodes"
                  ? `S${String(item.season_num || 0).padStart(2, "0")}E${String(item.episode_num || 0).padStart(2, "0")}`
                  : mode === "seasons"
                    ? "Season"
                    : item.year || item.type;
            const mainAction = mode === "shows"
              ? () => openSeasons(item)
              : mode === "seasons"
                ? () => openEpisodes(item)
                : () => actions.playMedia(item);

            if (mode === "shows" || mode === "seasons") {
              return (
                <div className="media-row split" key={`${item.type}-${item.rating_key}`}>
                  <button className="media-main" type="button" disabled={isDisabled} onClick={mainAction}>
                    {item.artwork_url ? <img src={item.artwork_url} alt="" /> : <span className="thumb-fallback"><Clapperboard size={20} /></span>}
                    <span>
                      <strong>{item.title}</strong>
                      <small>{subtitle}</small>
                    </span>
                  </button>
                  <button className="media-random" type="button" disabled={isDisabled} onClick={() => actions.playMedia(item)}>
                    <RefreshCw size={18} />
                    <span>Random</span>
                  </button>
                </div>
              );
            }

            return (
              <button
                className="media-row"
                key={`${item.type}-${item.rating_key}`}
                type="button"
                disabled={isDisabled}
                onClick={mainAction}
              >
                {item.artwork_url ? <img src={item.artwork_url} alt="" /> : <span className="thumb-fallback"><Clapperboard size={20} /></span>}
                <span>
                  <strong>{item.title}</strong>
                  <small>{subtitle}</small>
                </span>
              </button>
            );
          })}
        </div>
      </section>
    );
  }

  return (
    <section className={`browser top ${echo ? "echo" : ""}`}>
      <div className="choice-grid">
        <button className="choice large" type="button" onClick={() => setMode("movies")}>
          <Clapperboard size={24} />
          <span>Movies</span>
        </button>
        <button className="choice large" type="button" onClick={openShows}>
          <Tv size={24} />
          <span>TV Shows</span>
        </button>
      </div>
      <div className="random-grid">
        <button className="choice random" type="button" onClick={actions.randomMovie} disabled={pending.has("random-movie")}>
          <RefreshCw size={22} />
          <span>Random Movie</span>
        </button>
        <button className="choice random" type="button" onClick={() => actions.randomShow(shows)} disabled={pending.has("random-show") || shows.filter((item) => !item.unavailable).length === 0}>
          <RefreshCw size={22} />
          <span>Random TV</span>
        </button>
      </div>
    </section>
  );
}

function ServicePill({ label, value }) {
  return (
    <div className={`pill ${statusToneClass(value)}`}>
      <span>{label}</span>
      <strong>{statusLabel(value, "Running", "Stopped")}</strong>
    </div>
  );
}

function HomeView(props) {
  const { status, nowPlaying, actions, pending, movies, shows, error, lastUpdated, refreshAll } = props;
  return (
    <main className="app-page">
      <header className="topbar">
        <a className="brand" href="/"><Clapperboard size={22} />Plex Remote</a>
        <nav>
          <IconButton icon={Monitor} label="Echo" href="/echo" />
          <IconButton icon={Settings} label="Settings" href="/settings" />
        </nav>
      </header>
      <section className="dashboard-grid">
        <NowPlayingPanel nowPlaying={nowPlaying} actions={actions} pending={pending} />
        <MediaBrowser movies={movies} shows={shows} actions={actions} pending={pending} />
      </section>
      <section className="status-strip">
        <ServicePill label="TV" value={status?.tv_status} />
        <ServicePill label="Tailscale" value={status?.tailscale_connected} />
        <ServicePill label="Plex HTPC" value={status?.plex_htpc_running} />
        <ServicePill label="Sunshine" value={status?.sunshine_running} />
        <button className="text-button" type="button" onClick={refreshAll}>
          <RefreshCw size={16} />{lastUpdated ? lastUpdated.toLocaleTimeString() : "Refresh"}
        </button>
      </section>
      {error && <div className="toast">{error}</div>}
    </main>
  );
}

function EchoView(props) {
  const { nowPlaying, actions, pending, movies, shows, error } = props;
  return (
    <main className="echo-page">
      <NowPlayingPanel nowPlaying={nowPlaying} actions={actions} pending={pending} compact />
      <MediaBrowser movies={movies} shows={shows} actions={actions} pending={pending} echo />
      {error && <div className="toast echo-toast">{error}</div>}
    </main>
  );
}

function SettingsRow({ icon: Icon, label, value, toneValue, children }) {
  return (
    <div className="settings-row">
      <div className="settings-label">
        <Icon size={20} />
        <span>{label}</span>
      </div>
      <strong className={statusToneClass(toneValue)}>{value}</strong>
      <div className="settings-actions">{children}</div>
    </div>
  );
}

function SettingsView({ status, actions, pending, error, lastUpdated, refreshAll }) {
  return (
    <main className="app-page settings-page">
      <header className="topbar">
        <a className="brand" href="/"><Home size={22} />Plex Remote</a>
        <nav>
          <IconButton icon={Monitor} label="Echo" href="/echo" />
          <IconButton icon={RefreshCw} label="Refresh" onClick={refreshAll} />
        </nav>
      </header>
      <section className="settings-panel">
        <h1>Settings</h1>
        <SettingsRow icon={Power} label="TV Power" value={statusLabel(status?.tv_status)} toneValue={status?.tv_status}>
          <button type="button" onClick={actions.tvOn} disabled={pending.has("tv-on")}>On</button>
          <button type="button" onClick={actions.tvOff} disabled={pending.has("tv-off")}>Off</button>
        </SettingsRow>
        <SettingsRow icon={Monitor} label="TV Input" value={status?.tv_source ? `HDMI ${status.tv_source}` : "Unknown"}>
          <button type="button" onClick={actions.hdmi3} disabled={pending.has("hdmi3")}>HDMI 3</button>
        </SettingsRow>
        <SettingsRow icon={Volume2} label="TV Volume" value={status?.tv_volume == null ? "Unknown" : `${status.tv_volume}%`}>
          <button type="button" onClick={actions.volumeDown} disabled={pending.has("volume-down")}><Volume1 size={16} /></button>
          <button type="button" onClick={actions.volumeUp} disabled={pending.has("volume-up")}><Volume2 size={16} /></button>
        </SettingsRow>
        <SettingsRow icon={Clapperboard} label="Plex HTPC" value={statusLabel(status?.plex_htpc_running, "Running", "Stopped")} toneValue={status?.plex_htpc_running}>
          <button type="button" onClick={actions.plexStart} disabled={pending.has("plex-start")}>Start</button>
          <button type="button" onClick={actions.plexTerminate} disabled={pending.has("plex-terminate")}>Terminate</button>
        </SettingsRow>
        <SettingsRow icon={Network} label="Tailscale" value={statusLabel(status?.tailscale_connected, "Connected", "Disconnected")} toneValue={status?.tailscale_connected}>
          <button type="button" onClick={actions.tailscaleStart} disabled={pending.has("tailscale-start")}>Start</button>
          <button type="button" onClick={actions.tailscaleStop} disabled={pending.has("tailscale-stop")}>Stop</button>
        </SettingsRow>
        <SettingsRow icon={Monitor} label="Sunshine" value={statusLabel(status?.sunshine_running, "Running", "Stopped")} toneValue={status?.sunshine_running}>
          <button type="button" onClick={actions.sunshineStart} disabled={pending.has("sunshine-start")}>Start</button>
          <button type="button" onClick={actions.sunshineStop} disabled={pending.has("sunshine-stop")}>Stop</button>
        </SettingsRow>
        <p className="settings-time">{lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : ""}</p>
      </section>
      {error && <div className="toast">{error}</div>}
    </main>
  );
}

function App() {
  const remote = usePlexRemote();
  const route = currentRoute();
  if (route === "echo") return <EchoView {...remote} />;
  if (route === "settings") return <SettingsView {...remote} />;
  return <HomeView {...remote} />;
}

createRoot(document.getElementById("root")).render(<App />);
