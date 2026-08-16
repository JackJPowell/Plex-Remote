import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Armchair,
  ArrowLeft,
  BedDouble,
  Check,
  ChevronDown,
  ChevronUp,
  Clapperboard,
  Clock,
  CircleHelp,
  Edit2,
  Home,
  List,
  MessageSquare,
  Minus,
  Monitor,
  Network,
  Pause,
  Play,
  Plus,
  Power,
  RefreshCw,
  Save,
  ScrollText,
  Settings,
  SkipForward,
  Square,
  Trash2,
  Tv,
  Volume1,
  Volume2,
  X
} from "lucide-react";
import "./styles.css";

const EMPTY_NOW_PLAYING = {
  playing: false,
  state: null,
  display_title: null,
  artwork_url: null,
  progress_percent: null
};

const EMPTY_PLAYBACK_STATE = {
  queue: [],
  timer: { active: false, expires_at: null, remaining_seconds: 0 },
  active_messages: [],
  recovery: { active: false, stage: null },
  now_playing: null
};

const EMPTY_OCCUPANCY_STATE = {
  configured: false,
  connected: false,
  zones: {
    chair: { occupied: null, state: null },
    bed: { occupied: null, state: null }
  }
};

const STATUS_HOLD_MS = 18000;
const PLAY_START_HOLD_MS = 9000;
const COMMAND_UNLOCK_MS = 650;
const MEDIA_BROWSER_IDLE_MS = 2 * 60 * 1000;
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
    // Storage is only for per-client optimistic display holds.
  }
}

function statusFromHolds(holds) {
  return Object.fromEntries(
    Object.entries(holds).map(([key, hold]) => [key, hold.value])
  );
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {})
    }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

async function getJson(url, signal) {
  return requestJson(url, { signal });
}

async function postJson(url, body) {
  return requestJson(url, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body)
  });
}

async function putJson(url, body) {
  return requestJson(url, { method: "PUT", body: JSON.stringify(body) });
}

async function deleteJson(url) {
  return requestJson(url, { method: "DELETE" });
}

function currentRoute() {
  const path = window.location.pathname;
  if (path === "/echo") return "echo";
  if (path === "/settings") return "settings";
  if (path === "/messages") return "messages";
  if (path === "/logs") return "logs";
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

function timerLabel(timer) {
  const seconds = Number(timer?.remaining_seconds || 0);
  if (!timer?.active || seconds <= 0) return "Off";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  if (hours <= 0) return `${minutes}m`;
  if (minutes >= 60) return `${hours + 1}h`;
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
}

function dateTimeInputValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function defaultMessageForm() {
  const start = new Date(Date.now() + 5 * 60000);
  const end = new Date(start.getTime() + 2 * 60 * 60000);
  return {
    text: "",
    starts_at: dateTimeInputValue(start.toISOString()),
    ends_at: dateTimeInputValue(end.toISOString()),
    enabled: true
  };
}

function localDateTimeValue(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
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
  const [playbackState, setPlaybackState] = useState(EMPTY_PLAYBACK_STATE);
  const [occupancy, setOccupancy] = useState(EMPTY_OCCUPANCY_STATE);
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

  const refreshPlaybackState = useCallback(async (signal) => {
    const data = await getJson("/playback/state", signal);
    const queue = Array.isArray(data.queue) ? data.queue : [];
    const activeMessages = Array.isArray(data.active_messages) ? data.active_messages : [];
    setPlaybackState({
      ...EMPTY_PLAYBACK_STATE,
      ...data,
      timer: { ...EMPTY_PLAYBACK_STATE.timer, ...(data.timer || {}) },
      recovery: { ...EMPTY_PLAYBACK_STATE.recovery, ...(data.recovery || {}) },
      queue,
      active_messages: activeMessages
    });
    if (data.now_playing) applyNowPlaying(data.now_playing);
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

  const refreshOccupancy = useCallback(async (signal) => {
    const data = await getJson("/home-assistant/occupancy", signal);
    setOccupancy({
      ...EMPTY_OCCUPANCY_STATE,
      ...data,
      zones: { ...EMPTY_OCCUPANCY_STATE.zones, ...(data.zones || {}) }
    });
  }, []);

  const refreshAll = useCallback(async () => {
    const controller = new AbortController();
    try {
      await Promise.all([
        refreshStatus(controller.signal),
        refreshPlaybackState(controller.signal),
        refreshOccupancy(controller.signal),
        refreshCatalog(controller.signal)
      ]);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
    return () => controller.abort();
  }, [refreshCatalog, refreshOccupancy, refreshPlaybackState, refreshStatus]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      refreshStatus(controller.signal),
      refreshPlaybackState(controller.signal),
      refreshOccupancy(controller.signal),
      refreshCatalog(controller.signal)
    ]).catch((err) => setError(err.message));
    return () => controller.abort();
  }, [refreshCatalog, refreshOccupancy, refreshPlaybackState, refreshStatus]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const controller = new AbortController();
      Promise.all([
        refreshStatus(controller.signal),
        refreshPlaybackState(controller.signal),
        refreshOccupancy(controller.signal)
      ]).catch((err) => setError(err.message));
    }, 3500);
    return () => window.clearInterval(id);
  }, [refreshOccupancy, refreshPlaybackState, refreshStatus]);

  const afterSharedChange = useCallback(() => {
    const controller = new AbortController();
    Promise.all([
      refreshPlaybackState(controller.signal),
      refreshNowPlaying(controller.signal)
    ]).catch((err) => setError(err.message));
  }, [refreshNowPlaying, refreshPlaybackState]);

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
      window.setTimeout(afterSharedChange, 550);
    } catch (err) {
      setError(err.message);
      afterSharedChange();
    } finally {
      window.clearTimeout(unlock);
      setPending((old) => {
        const next = new Set(old);
        next.delete(key);
        return next;
      });
    }
  }, [afterSharedChange, holdNowPlaying, holdStatus]);

  const sharedCommand = useCallback(async (key, run) => {
    setPending((old) => new Set(old).add(key));
    setError(null);
    try {
      const result = await run();
      afterSharedChange();
      return result;
    } catch (err) {
      setError(err.message);
      throw err;
    } finally {
      setPending((old) => {
        const next = new Set(old);
        next.delete(key);
        return next;
      });
    }
  }, [afterSharedChange]);

  const actions = useMemo(() => ({
    pause: () => command("pause", "/plex/pause", ({ setNowPlaying }) => {
      setNowPlaying((old) => ({ ...old, state: "paused", playing: true }));
    }),
    resume: () => command("resume", "/plex/resume", ({ setNowPlaying }) => {
      setNowPlaying((old) => ({ ...old, state: "playing", playing: true }));
    }),
    stop: () => command("stop", "/plex/stop", ({ setNowPlaying }) => setNowPlaying(EMPTY_NOW_PLAYING)),
    next: () => {
      const firstQueued = playbackState.queue[0];
      if (firstQueued) {
        return sharedCommand(`queue-play-${firstQueued.id}`, () => postJson(`/playback/queue/${firstQueued.id}/play-now`));
      }
      return command("next", "/plex/play", ({ holdNowPlaying, holdStatus }) => {
        holdStatus({ plex_htpc_running: true });
        holdNowPlaying({ playing: true, state: "starting", display_title: "Starting..." });
      });
    },
    randomMovie: () => command("random-movie", "/plex/play", ({ holdNowPlaying, holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
      holdNowPlaying({ playing: true, state: "starting", display_title: "Starting movie..." });
    }),
    randomShow: () => command("random-show", "/plex/play?random_mode=show", ({ holdNowPlaying, holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
      holdNowPlaying({ playing: true, state: "starting", display_title: "Starting random TV..." });
    }),
    requestHelp: () => command("request-help", "/home-assistant/help"),
    playMedia: (item) => command(`media-${item.rating_key}`, `/plex/play?media_id=${item.rating_key}`, ({ holdNowPlaying, holdStatus }) => {
      holdStatus({ plex_htpc_running: true });
      holdNowPlaying({ playing: true, state: "starting", display_title: item.type === "show" ? `${item.title}: random episode` : item.title, artwork_url: item.artwork_url });
    }),
    addToQueue: (item) => sharedCommand(`queue-add-${item.rating_key}`, () => postJson("/playback/queue", {
      media_id: Number(item.rating_key),
      title: item.type === "show" ? `${item.title}: random episode` : item.title,
      media_type: item.type,
      artwork_url: item.artwork_url
    })),
    removeQueueItem: (item) => sharedCommand(`queue-remove-${item.id}`, () => deleteJson(`/playback/queue/${item.id}`)),
    clearQueue: () => sharedCommand("queue-clear", () => deleteJson("/playback/queue")),
    moveQueueItem: (item, direction) => {
      const queue = playbackState.queue;
      const index = queue.findIndex((entry) => entry.id === item.id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= queue.length) return undefined;
      const ids = queue.map((entry) => entry.id);
      [ids[index], ids[target]] = [ids[target], ids[index]];
      return sharedCommand(`queue-move-${item.id}`, () => postJson("/playback/queue/reorder", { ids }));
    },
    playQueueItemNow: (item) => sharedCommand(`queue-play-${item.id}`, () => postJson(`/playback/queue/${item.id}/play-now`)),
    adjustTimer: (hours) => sharedCommand(`timer-${hours > 0 ? "plus" : "minus"}`, () => postJson("/playback/timer", { hours_delta: hours })),
    clearTimer: () => sharedCommand("timer-clear", () => postJson("/playback/timer", { clear: true })),
    seek: (percent) => sharedCommand("seek", async () => {
      const result = await postJson("/plex/seek", { percent });
      setNowPlaying((old) => ({
        ...old,
        progress: result.offset,
        progress_percent: result.percent
      }));
      return result;
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
    }),
    refreshPlaybackState
  }), [command, playbackState.queue, refreshPlaybackState, sharedCommand]);

  return {
    status,
    nowPlaying,
    playbackState,
    occupancy,
    movies,
    shows,
    error,
    pending,
    lastUpdated,
    actions,
    refreshAll,
    setError
  };
}

function TimerControls({ timer, actions, pending }) {
  return (
    <div className="timer-controls" aria-label="Playback timer">
      <Clock size={17} />
      <strong>{timerLabel(timer)}</strong>
      <button type="button" onClick={() => actions.adjustTimer(-1)} disabled={pending.has("timer-minus") || !timer?.active} aria-label="Subtract one hour">
        <Minus size={16} />
      </button>
      <button type="button" onClick={() => actions.adjustTimer(1)} disabled={pending.has("timer-plus")} aria-label="Add one hour">
        <Plus size={16} />
      </button>
      <button type="button" onClick={actions.clearTimer} disabled={pending.has("timer-clear") || !timer?.active} aria-label="Clear timer">
        <X size={16} />
      </button>
    </div>
  );
}

function NowPlayingPanel({ nowPlaying, playbackState, actions, pending, compact = false, onQueueOpen, tvStatus }) {
  const [rotationIndex, setRotationIndex] = useState(0);
  const [seekProgress, setSeekProgress] = useState(null);
  const messages = Array.isArray(playbackState?.active_messages) ? playbackState.active_messages : [];
  const queue = Array.isArray(playbackState?.queue) ? playbackState.queue : [];

  useEffect(() => {
    if (messages.length === 0) {
      setRotationIndex(0);
      return undefined;
    }
    const id = window.setInterval(() => {
      setRotationIndex((old) => (old + 1) % (messages.length + 1));
    }, 7000);
    return () => window.clearInterval(id);
  }, [messages.length]);

  const state = String(nowPlaying?.state || "").toLowerCase();
  const hasMedia = Boolean(nowPlaying?.state || nowPlaying?.display_title);
  const isPlaying = state === "playing" || state === "buffering";
  const progress = Number.isFinite(Number(nowPlaying?.progress_percent))
    ? Math.max(0, Math.min(100, Number(nowPlaying.progress_percent)))
    : 0;
  const canSeek = hasMedia && Number(nowPlaying?.duration) > 0;

  useEffect(() => {
    setSeekProgress(null);
  }, [nowPlaying?.progress_percent]);

  const commitSeek = useCallback((value) => {
    if (!canSeek || pending.has("seek")) return;
    actions.seek(Number(value));
  }, [actions, canSeek, pending]);
  const playPause = isPlaying
    ? { icon: Pause, label: "Pause", action: actions.pause, key: "pause" }
    : state === "paused"
      ? { icon: Play, label: "Play", action: actions.resume, key: "resume" }
      : { icon: Play, label: "Play", action: actions.randomMovie, key: "random-movie" };
  const message = messages.length > 0 && rotationIndex > 0
    ? messages[(rotationIndex - 1) % messages.length]
    : null;
  const tvIsOn = tvStatus === "on" || tvStatus === true;
  const tvPowerPending = pending.has("tv-on") || pending.has("tv-off");

  return (
    <section className={`now-panel ${compact ? "compact" : ""}`}>
      <div className="control-rail">
        <IconButton icon={playPause.icon} label={playPause.label} onClick={playPause.action} disabled={pending.has(playPause.key)} active />
        <IconButton icon={Square} label="Stop" onClick={actions.stop} disabled={pending.has("stop")} />
        <IconButton icon={SkipForward} label="Next" onClick={actions.next} disabled={pending.has("next")} />
        {compact && onQueueOpen && <div className="control-gap" aria-hidden="true" />}
        {compact && onQueueOpen && <IconButton icon={List} label="Queue" onClick={onQueueOpen} active={queue.length > 0} />}
        {compact && <IconButton icon={Power} label={`Turn TV ${tvIsOn ? "off" : "on"}`} onClick={tvIsOn ? actions.tvOff : actions.tvOn} disabled={tvPowerPending} active={tvIsOn} />}
        {compact && <IconButton icon={Settings} label="Settings" href="/settings" />}
      </div>
      <div className="now-content">
        <div className={`rotating-pane ${message ? "showing-message" : ""}`}>
          {message ? (
            <div className="message-display">
              <MessageSquare size={34} />
              <div className="eyebrow">Reminder</div>
              <h1>{message.text}</h1>
              <div className="state-line">Active until {new Date(message.ends_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</div>
            </div>
          ) : (
            <>
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
                <div className="state-line">{hasMedia ? statusLabel(nowPlaying.state, "Playing", "Paused") : "Idle"}</div>
                <h1>{titleFor(nowPlaying)}</h1>
                <input
                  className="progress-track"
                  type="range"
                  min="0"
                  max="100"
                  step="0.1"
                  value={seekProgress ?? progress}
                  style={{ "--progress": `${seekProgress ?? progress}%` }}
                  onChange={(event) => setSeekProgress(Number(event.target.value))}
                  onPointerUp={(event) => commitSeek(event.currentTarget.value)}
                  onKeyUp={(event) => {
                    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
                      commitSeek(event.currentTarget.value);
                    }
                  }}
                  disabled={!canSeek || pending.has("seek")}
                  aria-label="Playback progress"
                  aria-valuetext={`${Math.round(seekProgress ?? progress)} percent`}
                />
              </div>
            </>
          )}
        </div>
        <TimerControls timer={playbackState?.timer} actions={actions} pending={pending} />
      </div>
    </section>
  );
}

function MediaChoiceModal({ item, onClose, actions, pending }) {
  if (!item) return null;
  const title = item.type === "show" ? `${item.title}: random episode` : item.title;
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="choice-modal" role="dialog" aria-modal="true" aria-labelledby="media-choice-title">
        <button className="modal-close" type="button" onClick={onClose} aria-label="Close"><X size={18} /></button>
        <h2 id="media-choice-title">{title}</h2>
        <p>Something is already playing.</p>
        <div className="modal-actions">
          <button type="button" onClick={() => { actions.playMedia(item); onClose(); }} disabled={pending.has(`media-${item.rating_key}`)}>
            <Play size={17} />Start now
          </button>
          <button type="button" onClick={() => { actions.addToQueue(item); onClose(); }} disabled={pending.has(`queue-add-${item.rating_key}`)}>
            <List size={17} />Add to queue
          </button>
        </div>
      </div>
    </div>
  );
}

function MediaBrowser({ movies, shows, actions, pending, nowPlaying, echo = false }) {
  const [mode, setMode] = useState("top");
  const [selectedShow, setSelectedShow] = useState(null);
  const [selectedSeason, setSelectedSeason] = useState(null);
  const [seasons, setSeasons] = useState([]);
  const [episodes, setEpisodes] = useState([]);
  const [loading, setLoading] = useState(false);
  const [browserError, setBrowserError] = useState(null);
  const [choiceItem, setChoiceItem] = useState(null);
  const [helpRequested, setHelpRequested] = useState(false);
  const idleTimer = useRef(null);
  const helpResetTimer = useRef(null);
  const navigationId = useRef(0);
  const playbackActive = Boolean(nowPlaying?.playing || nowPlaying?.state);
  const title = mode === "movies"
    ? "Movies"
    : mode === "seasons"
      ? selectedShow?.title || "Seasons"
      : mode === "episodes"
        ? selectedSeason?.title || "Episodes"
        : "TV Shows";

  const returnToTop = useCallback(() => {
    navigationId.current += 1;
    setMode("top");
    setSelectedShow(null);
    setSelectedSeason(null);
    setSeasons([]);
    setEpisodes([]);
    setLoading(false);
    setBrowserError(null);
    setChoiceItem(null);
  }, []);

  useEffect(() => () => window.clearTimeout(helpResetTimer.current), []);

  const requestHelp = () => {
    setHelpRequested(true);
    window.clearTimeout(helpResetTimer.current);
    helpResetTimer.current = window.setTimeout(() => setHelpRequested(false), 10000);
    actions.requestHelp();
  };

  const resetIdleTimer = useCallback(() => {
    window.clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(returnToTop, MEDIA_BROWSER_IDLE_MS);
  }, [returnToTop]);

  useEffect(() => {
    if (mode === "top") {
      window.clearTimeout(idleTimer.current);
      idleTimer.current = null;
      return undefined;
    }

    resetIdleTimer();
    window.addEventListener("click", resetIdleTimer, true);
    return () => {
      window.removeEventListener("click", resetIdleTimer, true);
      window.clearTimeout(idleTimer.current);
      idleTimer.current = null;
    };
  }, [mode, resetIdleTimer]);

  const chooseMedia = (item) => {
    if (playbackActive) {
      setChoiceItem(item);
    } else {
      actions.playMedia(item);
    }
  };

  const openShows = () => {
    navigationId.current += 1;
    setSelectedShow(null);
    setSelectedSeason(null);
    setSeasons([]);
    setEpisodes([]);
    setMode("shows");
  };

  const goBack = () => {
    navigationId.current += 1;
    setLoading(false);
    setBrowserError(null);
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
    returnToTop();
  };

  const openSeasons = async (show) => {
    const requestNavigationId = ++navigationId.current;
    setSelectedShow(show);
    setSelectedSeason(null);
    setEpisodes([]);
    setLoading(true);
    setBrowserError(null);
    try {
      const data = await getJson(`/plex/media/shows/${show.rating_key}/seasons`);
      if (navigationId.current !== requestNavigationId) return;
      setSeasons(data);
      setMode("seasons");
    } catch (err) {
      if (navigationId.current !== requestNavigationId) return;
      setBrowserError(err.message);
    } finally {
      if (navigationId.current === requestNavigationId) setLoading(false);
    }
  };

  const openEpisodes = async (season) => {
    const requestNavigationId = ++navigationId.current;
    setSelectedSeason(season);
    setLoading(true);
    setBrowserError(null);
    try {
      const data = await getJson(`/plex/media/shows/${selectedShow.rating_key}/seasons/${season.rating_key}/episodes`);
      if (navigationId.current !== requestNavigationId) return;
      setEpisodes(data);
      setMode("episodes");
    } catch (err) {
      if (navigationId.current !== requestNavigationId) return;
      setBrowserError(err.message);
    } finally {
      if (navigationId.current === requestNavigationId) setLoading(false);
    }
  };

  const content = mode !== "top" ? (
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
        ) : (mode === "movies" ? movies : mode === "shows" ? shows : mode === "seasons" ? seasons : episodes).length === 0 ? (
          <div className="empty-list">No items configured</div>
        ) : (mode === "movies" ? movies : mode === "shows" ? shows : mode === "seasons" ? seasons : episodes).map((item) => {
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
              : () => chooseMedia(item);

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
                <button className="media-random" type="button" disabled={isDisabled} onClick={() => chooseMedia(item)}>
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
  ) : (
    <section className={`browser top ${echo ? "echo" : ""}`}>
      <div className="choice-grid">
        <button className="choice large" type="button" onClick={() => { navigationId.current += 1; setMode("movies"); }}>
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
        <button className="choice random" type="button" onClick={actions.randomShow} disabled={pending.has("random-show") || shows.filter((item) => !item.unavailable).length === 0}>
          <RefreshCw size={22} />
          <span>Random TV</span>
        </button>
      </div>
      <button className="choice help" type="button" onClick={requestHelp} disabled={pending.has("request-help")}>
        {helpRequested ? <Check size={23} /> : <CircleHelp size={23} />}
        <span>{helpRequested ? "Jack has been notified" : "Something not right? Click here"}</span>
      </button>
    </section>
  );

  return (
    <>
      {content}
      <MediaChoiceModal item={choiceItem} onClose={() => setChoiceItem(null)} actions={actions} pending={pending} />
    </>
  );
}

function QueueDrawer({ open, onClose, queue, actions, pending }) {
  if (!open) return null;
  return (
    <div className="drawer-backdrop" role="presentation" onClick={onClose}>
      <aside className="queue-drawer" aria-label="Playback queue" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <h2>Queue</h2>
          <IconButton icon={X} label="Close queue" onClick={onClose} />
        </div>
        <div className="queue-list">
          {queue.length === 0 ? (
            <div className="empty-list">No queued media</div>
          ) : queue.map((item, index) => (
            <div className="queue-item" key={item.id}>
              {item.artwork_url ? <img src={item.artwork_url} alt="" /> : <span className="thumb-fallback"><Clapperboard size={20} /></span>}
              <span>
                <strong>{item.title || `Media ${item.media_id}`}</strong>
                <small>{item.type || "queued"}</small>
              </span>
              <div className="queue-actions">
                <button type="button" onClick={() => actions.moveQueueItem(item, -1)} disabled={index === 0 || pending.has(`queue-move-${item.id}`)} aria-label="Move up"><ChevronUp size={16} /></button>
                <button type="button" onClick={() => actions.moveQueueItem(item, 1)} disabled={index === queue.length - 1 || pending.has(`queue-move-${item.id}`)} aria-label="Move down"><ChevronDown size={16} /></button>
                <button type="button" onClick={() => actions.playQueueItemNow(item)} disabled={pending.has(`queue-play-${item.id}`)} aria-label="Play now"><Play size={16} /></button>
                <button type="button" onClick={() => actions.removeQueueItem(item)} disabled={pending.has(`queue-remove-${item.id}`)} aria-label="Remove"><Trash2 size={16} /></button>
              </div>
            </div>
          ))}
        </div>
        <button className="danger-button" type="button" onClick={actions.clearQueue} disabled={queue.length === 0 || pending.has("queue-clear")}>
          <Trash2 size={16} />Clear queue
        </button>
      </aside>
    </div>
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

function OccupancyButton({ icon, label, zone, connected }) {
  const occupied = zone?.occupied;
  const stateLabel = occupied === true ? "occupied" : occupied === false ? "unoccupied" : "unknown";
  const connectionLabel = connected ? "" : " (Home Assistant disconnected)";
  return (
    <IconButton
      icon={icon}
      label={`${label}: ${stateLabel}${connectionLabel}`}
      active={connected && occupied === true}
      tone={!connected || occupied === null || occupied === undefined ? "occupancy-unknown" : "occupancy-status"}
    />
  );
}

function HomeView(props) {
  const { status, nowPlaying, playbackState, occupancy, actions, pending, movies, shows, error, lastUpdated, refreshAll } = props;
  const [queueOpen, setQueueOpen] = useState(false);
  return (
    <main className="app-page">
      <header className="topbar">
        <a className="brand" href="/"><Clapperboard size={22} />Plex Remote</a>
        <nav>
          <OccupancyButton icon={Armchair} label="Chair" zone={occupancy.zones.chair} connected={occupancy.connected} />
          <OccupancyButton icon={BedDouble} label="Bed" zone={occupancy.zones.bed} connected={occupancy.connected} />
          <IconButton icon={List} label="Queue" onClick={() => setQueueOpen(true)} active={playbackState.queue.length > 0} />
          <IconButton icon={MessageSquare} label="Messages" href="/messages" active={playbackState.active_messages.length > 0} />
          <IconButton icon={ScrollText} label="Logs" href="/logs" />
          <IconButton icon={Monitor} label="Echo" href="/echo" />
          <IconButton icon={Settings} label="Settings" href="/settings" />
        </nav>
      </header>
      <section className="dashboard-grid">
        <NowPlayingPanel nowPlaying={nowPlaying} playbackState={playbackState} actions={actions} pending={pending} />
        <MediaBrowser movies={movies} shows={shows} actions={actions} pending={pending} nowPlaying={nowPlaying} />
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
      <QueueDrawer open={queueOpen} onClose={() => setQueueOpen(false)} queue={playbackState.queue} actions={actions} pending={pending} />
    </main>
  );
}

function EchoView(props) {
  const { status, nowPlaying, playbackState, actions, pending, movies, shows, error } = props;
  const [queueOpen, setQueueOpen] = useState(false);
  return (
    <main className="echo-page">
      <NowPlayingPanel nowPlaying={nowPlaying} playbackState={playbackState} actions={actions} pending={pending} compact onQueueOpen={() => setQueueOpen(true)} tvStatus={status?.tv_status} />
      <MediaBrowser movies={movies} shows={shows} actions={actions} pending={pending} nowPlaying={nowPlaying} echo />
      <QueueDrawer open={queueOpen} onClose={() => setQueueOpen(false)} queue={playbackState.queue} actions={actions} pending={pending} />
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
          <IconButton icon={MessageSquare} label="Messages" href="/messages" />
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
    </main>
  );
}

function MessagesView({ error, setError, playbackState, refreshAll }) {
  const [messages, setMessages] = useState([]);
  const [form, setForm] = useState(defaultMessageForm);
  const [editingId, setEditingId] = useState(null);
  const [loading, setLoading] = useState(false);

  const loadMessages = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getJson("/messages");
      setMessages(Array.isArray(data) ? data : []);
      setError(null);
    } catch (err) {
      setError(err.message);
      setMessages([]);
    } finally {
      setLoading(false);
    }
  }, [setError]);

  useEffect(() => {
    loadMessages();
  }, [loadMessages]);

  const resetForm = () => {
    setEditingId(null);
    setForm(defaultMessageForm());
  };

  const submit = async (event) => {
    event.preventDefault();
    const payload = {
      text: form.text,
      starts_at: form.starts_at,
      ends_at: form.ends_at,
      enabled: form.enabled
    };
    try {
      if (editingId) {
        await putJson(`/messages/${editingId}`, payload);
      } else {
        await postJson("/messages", payload);
      }
      resetForm();
      await loadMessages();
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  };

  const edit = (message) => {
    setEditingId(message.id);
    setForm({
      text: message.text,
      starts_at: dateTimeInputValue(message.starts_at),
      ends_at: dateTimeInputValue(message.ends_at),
      enabled: message.enabled
    });
  };

  const remove = async (message) => {
    try {
      await deleteJson(`/messages/${message.id}`);
      await loadMessages();
      refreshAll();
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <main className="app-page settings-page">
      <header className="topbar">
        <a className="brand" href="/"><Home size={22} />Plex Remote</a>
        <nav>
          <IconButton icon={Monitor} label="Echo" href="/echo" />
          <IconButton icon={Settings} label="Settings" href="/settings" />
        </nav>
      </header>
      <section className="messages-layout">
        <form className="message-form" onSubmit={submit}>
          <h1>{editingId ? "Edit Message" : "New Message"}</h1>
          <label>
            <span>Message</span>
            <textarea value={form.text} onChange={(event) => setForm((old) => ({ ...old, text: event.target.value }))} required rows={5} />
          </label>
          <div className="time-fields">
            <label>
              <span>Start</span>
              <input type="datetime-local" value={form.starts_at} onChange={(event) => setForm((old) => ({ ...old, starts_at: event.target.value }))} required />
            </label>
            <label>
              <span>End</span>
              <input type="datetime-local" value={form.ends_at} onChange={(event) => setForm((old) => ({ ...old, ends_at: event.target.value }))} required />
            </label>
          </div>
          <label className="toggle-row">
            <input type="checkbox" checked={form.enabled} onChange={(event) => setForm((old) => ({ ...old, enabled: event.target.checked }))} />
            <span>Enabled</span>
          </label>
          <div className="form-actions">
            <button type="submit"><Save size={16} />{editingId ? "Save" : "Add"}</button>
            {editingId && <button type="button" onClick={resetForm}><X size={16} />Cancel</button>}
          </div>
        </form>
        <section className="messages-list-panel">
          <div className="browser-head">
            <MessageSquare size={20} />
            <h2>Messages</h2>
            <span className="active-count">{Array.isArray(playbackState.active_messages) ? playbackState.active_messages.length : 0} active</span>
          </div>
          <div className="message-list">
            {loading ? (
              <div className="empty-list">Loading</div>
            ) : messages.length === 0 ? (
              <div className="empty-list">No messages scheduled</div>
            ) : messages.map((message) => (
              <article className={`message-card ${message.active ? "active" : ""}`} key={message.id}>
                <div>
                  <strong>{message.text}</strong>
                  <small>
                    {new Date(message.starts_at).toLocaleString()} - {new Date(message.ends_at).toLocaleString()}
                  </small>
                </div>
                <div className="message-card-actions">
                  {message.active && <span title="Active"><Check size={16} /></span>}
                  <button type="button" onClick={() => edit(message)} aria-label="Edit"><Edit2 size={16} /></button>
                  <button type="button" onClick={() => remove(message)} aria-label="Delete"><Trash2 size={16} /></button>
                </div>
              </article>
            ))}
          </div>
        </section>
      </section>
    </main>
  );
}

function LogsView() {
  const now = new Date();
  const [filters, setFilters] = useState(() => ({
    start: localDateTimeValue(new Date(now.getTime() - 7 * 86400000)),
    end: localDateTimeValue(now),
    type: ""
  }));
  const [data, setData] = useState({ logs: [], counts: {}, total: 0 });
  const [loading, setLoading] = useState(false);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ start: filters.start, end: filters.end });
      if (filters.type) params.set("type", filters.type);
      const result = await getJson(`/api/logs?${params}`);
      setData(result);
    } catch {
      setData({ logs: [], counts: {}, total: 0 });
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  const types = Object.entries(data.counts || {});
  const totalForPeriod = types.reduce((sum, [, count]) => sum + count, 0);

  return (
    <main className="app-page settings-page">
      <header className="topbar">
        <a className="brand" href="/"><Home size={22} />Plex Remote</a>
        <nav>
          <IconButton icon={RefreshCw} label="Refresh" onClick={loadLogs} disabled={loading} />
        </nav>
      </header>
      <section className="logs-panel">
        <div className="logs-heading">
          <div><h1>Error Logs</h1><p>Persisted application and request failures</p></div>
          <strong>{data.total} matching</strong>
        </div>
        <div className="log-filters">
          <label><span>From</span><input type="datetime-local" value={filters.start} onChange={(event) => setFilters((old) => ({ ...old, start: event.target.value }))} /></label>
          <label><span>To</span><input type="datetime-local" value={filters.end} onChange={(event) => setFilters((old) => ({ ...old, end: event.target.value }))} /></label>
          <label><span>Type</span><select value={filters.type} onChange={(event) => setFilters((old) => ({ ...old, type: event.target.value }))}><option value="">All types</option>{types.map(([type]) => <option value={type} key={type}>{type}</option>)}</select></label>
        </div>
        <div className="log-counts">
          <button type="button" className={!filters.type ? "active" : ""} onClick={() => setFilters((old) => ({ ...old, type: "" }))}>All <strong>{totalForPeriod}</strong></button>
          {types.map(([type, count]) => <button type="button" className={filters.type === type ? "active" : ""} onClick={() => setFilters((old) => ({ ...old, type }))} key={type}>{type} <strong>{count}</strong></button>)}
        </div>
        <div className="log-list">
          {loading ? <div className="empty-list">Loading</div> : data.logs.length === 0 ? <div className="empty-list">No errors found for this period</div> : data.logs.map((entry) => (
            <article className="log-entry" key={entry.id}>
              <div className="log-entry-head"><span className={`log-level ${entry.level}`}>{entry.level}</span><strong>{entry.type}</strong><time>{new Date(entry.created_at).toLocaleString()}</time></div>
              <p>{entry.message}</p>
              {(entry.method || entry.path || entry.status_code) && <code>{[entry.method, entry.path, entry.status_code].filter(Boolean).join(" · ")}</code>}
              {entry.details && <details><summary>Details</summary><pre>{entry.details}</pre></details>}
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}

function PlexRecoveryOverlay({ recovery }) {
  if (!recovery?.active) return null;
  return (
    <div className="plex-recovery-overlay" role="alertdialog" aria-modal="true" aria-label="Plex is restarting">
      <div className="plex-recovery-card">
        <RefreshCw className="recovery-spinner" size={34} aria-hidden="true" />
        <h1>Plex is restarting</h1>
        <p>{recovery.stage || "Restoring playback"}</p>
        <small>Please wait. Your selected media will start automatically.</small>
      </div>
    </div>
  );
}

function RemoteApp({ route }) {
  const remote = usePlexRemote();
  let view;
  if (route === "echo") view = <EchoView {...remote} />;
  else if (route === "settings") view = <SettingsView {...remote} />;
  else if (route === "messages") view = <MessagesView {...remote} />;
  else view = <HomeView {...remote} />;
  return <>{view}<PlexRecoveryOverlay recovery={remote.playbackState.recovery} /></>;
}

function App() {
  const route = currentRoute();
  return route === "logs" ? <LogsView /> : <RemoteApp route={route} />;
}

createRoot(document.getElementById("root")).render(<App />);
