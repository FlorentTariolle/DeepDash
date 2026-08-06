import "./styles.css";

import type {
  MainToWorkerMessage,
  RuntimeBackend,
  WorkerToMainMessage,
} from "./runtime/protocol";

function element<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!(node instanceof HTMLElement)) {
    throw new Error(`Missing player element #${id}.`);
  }
  return node as T;
}

const stage = element<HTMLDivElement>("stage");
const canvas = element<HTMLCanvasElement>("dream-canvas");
const context = canvas.getContext("2d", { alpha: false });
if (!context) {
  throw new Error("This browser cannot create a 2D canvas context.");
}
const canvasContext: CanvasRenderingContext2D = context;

const stageOverlay = element<HTMLDivElement>("stage-overlay");
const loadingPanel = element<HTMLDivElement>("loading-panel");
const startPanel = element<HTMLDivElement>("start-panel");
const endPanel = element<HTMLDivElement>("end-panel");
const errorPanel = element<HTMLDivElement>("error-panel");
const loadingKicker = element<HTMLParagraphElement>("loading-kicker");
const loadingTitle = element<HTMLHeadingElement>("loading-title");
const loadingDetail = element<HTMLParagraphElement>("loading-detail");
const loadProgress = element<HTMLProgressElement>("load-progress");
const progressFile = element<HTMLSpanElement>("progress-file");
const progressValue = element<HTMLSpanElement>("progress-value");
const endKicker = element<HTMLParagraphElement>("end-kicker");
const endTitle = element<HTMLHeadingElement>("end-title");
const endDetail = element<HTMLParagraphElement>("end-detail");
const errorDetail = element<HTMLParagraphElement>("error-detail");

const backendBadge = element<HTMLDivElement>("backend-badge");
const backendLabel = element<HTMLElement>("backend-label");
const liveStatus = element<HTMLParagraphElement>("live-status");
const stepLimit = element<HTMLSpanElement>("step-limit");
const hudStep = element<HTMLElement>("hud-step");
const hudAction = element<HTMLElement>("hud-action");
const hudDeath = element<HTMLElement>("hud-death");
const hudLatency = element<HTMLElement>("hud-latency");

const seedCounter = element<HTMLSpanElement>("seed-counter");
const seedPrevious = element<HTMLButtonElement>("seed-previous");
const seedNext = element<HTMLButtonElement>("seed-next");

const startButton = element<HTMLButtonElement>("start-button");
const playButton = element<HTMLButtonElement>("play-button");
const playButtonLabel = playButton.querySelector("span");
const stepButton = element<HTMLButtonElement>("step-button");
const retryButton = element<HTMLButtonElement>("retry-button");
const overlayRetry = element<HTMLButtonElement>("overlay-retry");
const overlayNext = element<HTMLButtonElement>("overlay-next");
const reloadButton = element<HTMLButtonElement>("reload-button");
const jumpButton = element<HTMLButtonElement>("jump-button");
const aiToggle = element<HTMLButtonElement>("ai-toggle");
const aiAvailability = element<HTMLSpanElement>("ai-availability");

const worker = new Worker(new URL("./runtime/dream-worker.ts", import.meta.url), {
  type: "module",
  name: "dashvmc-dream-runtime",
});

let isReady = false;
let isPlaying = false;
let isEnded = false;
let hasController = false;
let aiEnabled = false;
let activeBackend: RuntimeBackend | undefined;
let currentSeedIndex = 0;
let seedCount = 1;
const heldInputs = new Set<string>();

function send(message: MainToWorkerMessage): void {
  worker.postMessage(message);
}

function showPanel(panel: HTMLElement | null): void {
  for (const candidate of [loadingPanel, startPanel, endPanel, errorPanel]) {
    candidate.hidden = candidate !== panel;
  }
  stageOverlay.classList.toggle("is-hidden", panel === null);
}

function setLoadingCopy(kicker: string, title: string, detail: string): void {
  loadingKicker.textContent = kicker;
  loadingTitle.textContent = title;
  loadingDetail.textContent = detail;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "0 MB";
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(0)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function fileLabel(file: string): string {
  switch (file) {
    case "world.onnx":
      return "World model";
    case "decoder.onnx":
      return "Frame decoder";
    case "controller.onnx":
      return "AI pilot";
    case "seeds.bin":
      return "Dream seeds";
    default:
      return file;
  }
}

function setControlsEnabled(enabled: boolean): void {
  playButton.disabled = !enabled;
  stepButton.disabled = !enabled || isPlaying || isEnded;
  retryButton.disabled = !enabled;
  jumpButton.disabled = !enabled || aiEnabled || isEnded;
  seedPrevious.disabled = !enabled;
  seedNext.disabled = !enabled;
  aiToggle.disabled = !enabled || !hasController;
}

function updatePlaybackUi(): void {
  playButton.classList.toggle("is-playing", isPlaying);
  playButton.setAttribute("aria-label", isPlaying ? "Pause rollout" : "Play rollout");
  if (playButtonLabel) {
    playButtonLabel.textContent = isPlaying ? "Pause" : "Play";
  }
  aiToggle.setAttribute("aria-checked", String(aiEnabled));
  setControlsEnabled(isReady);
  if (aiEnabled) {
    releaseAllInputs();
  }
}

function updateSeedUi(index: number): void {
  const normalized = ((index % seedCount) + seedCount) % seedCount;
  currentSeedIndex = normalized;
  seedCounter.textContent = `${String(normalized + 1).padStart(2, "0")} / ${String(seedCount).padStart(2, "0")}`;
}

function setBackend(backend: RuntimeBackend): void {
  activeBackend = backend;
  backendBadge.dataset.backend = backend;
  const label = backend === "webgpu" ? "WebGPU" : "WASM CPU";
  backendLabel.textContent = label;
}

function drawFrame(message: Extract<WorkerToMainMessage, { type: "frame" }>): void {
  if (message.width !== 64 || message.height !== 64) {
    throw new Error(`Unexpected frame size ${message.width}×${message.height}.`);
  }
  const pixels = new Uint8ClampedArray(message.pixels);
  const expectedLength = message.width * message.height * 4;
  if (pixels.length !== expectedLength) {
    throw new Error(`Unexpected frame buffer length ${pixels.length}; expected ${expectedLength}.`);
  }
  canvasContext.putImageData(new ImageData(pixels, message.width, message.height), 0, 0);
  stage.classList.remove("is-loading");

  hudStep.textContent = String(message.step).padStart(2, "0");
  hudAction.textContent = message.action === 1 ? "Jump" : "Idle";
  hudAction.parentElement?.classList.toggle("is-jump", message.action === 1);
  hudDeath.textContent = `${Math.round(message.deathProbability * 100)}%`;
  hudLatency.textContent = message.latencyMs > 0 ? `${message.latencyMs.toFixed(0)} ms` : "—";
  stepLimit.textContent = `${message.step} ${message.step === 1 ? "step" : "steps"}`;
  updateSeedUi(message.seedIndex);

  if (message.ended) {
    isEnded = true;
    isPlaying = false;
    releaseAllInputs();
    endKicker.textContent = "Rollout ended";
    endTitle.textContent = "Dream over";
    endDetail.textContent = `The model predicted a collision at step ${message.step}.`;
    showPanel(endPanel);
    updatePlaybackUi();
  }
}

function setInputHeld(source: string, held: boolean): void {
  if (!isReady || isEnded || aiEnabled) {
    return;
  }
  if (held) {
    heldInputs.add(source);
  } else {
    heldInputs.delete(source);
  }
  const isHeld = heldInputs.size > 0;
  stage.classList.toggle("is-input-held", isHeld);
  jumpButton.classList.toggle("is-held", isHeld);
  jumpButton.setAttribute("aria-pressed", String(isHeld));
  hudAction.textContent = isHeld ? "Jump" : "Idle";
  hudAction.parentElement?.classList.toggle("is-jump", isHeld);
  send({ type: "set-action", held: isHeld });
}

function releaseAllInputs(): void {
  const wasHeld = heldInputs.size > 0;
  heldInputs.clear();
  stage.classList.remove("is-input-held");
  jumpButton.classList.remove("is-held");
  jumpButton.setAttribute("aria-pressed", "false");
  if (wasHeld) {
    send({ type: "set-action", held: false });
  }
}

function startPlayback(): void {
  if (!isReady) {
    return;
  }
  if (isEnded) {
    selectSeed(currentSeedIndex, true);
    return;
  }
  showPanel(null);
  send({ type: "play" });
}

function togglePlayback(): void {
  if (!isReady) {
    return;
  }
  if (isPlaying) {
    send({ type: "pause" });
  } else {
    startPlayback();
  }
}

function selectSeed(index: number, autoplay: boolean): void {
  if (!isReady || seedCount < 1) {
    return;
  }
  const normalized = ((index % seedCount) + seedCount) % seedCount;
  releaseAllInputs();
  isEnded = false;
  isPlaying = false;
  updateSeedUi(normalized);
  showPanel(autoplay ? null : startPanel);
  updatePlaybackUi();
  send({ type: "reset", seedIndex: normalized, autoplay });
}

function showError(message: string): void {
  isReady = false;
  isPlaying = false;
  releaseAllInputs();
  errorDetail.textContent = message;
  liveStatus.textContent = "Local inference could not start.";
  stage.classList.add("is-loading");
  showPanel(errorPanel);
  setControlsEnabled(false);
}

function handleStatus(message: Extract<WorkerToMainMessage, { type: "status" }>): void {
  liveStatus.textContent = message.detail;
  if (["capability", "downloading", "initializing", "fallback", "warmup"].includes(message.phase)) {
    stage.classList.add("is-loading");
    const kicker =
      message.phase === "downloading"
        ? "Local model download"
        : message.phase === "warmup"
          ? "One-time warm-up"
          : message.phase === "fallback"
            ? "Compatibility mode"
            : "Preparing local inference";
    setLoadingCopy(kicker, message.title, message.detail);
    showPanel(loadingPanel);
  } else if (message.phase === "playing") {
    showPanel(null);
  } else if (message.phase === "paused") {
    showPanel(null);
  }
}

function handleWorkerMessage(message: WorkerToMainMessage): void {
  switch (message.type) {
    case "status":
      handleStatus(message);
      break;
    case "progress": {
      const ratio = message.total > 0 ? Math.min(1, message.loaded / message.total) : 0;
      const percent = Math.round(ratio * 100);
      loadProgress.removeAttribute("value");
      if (message.total > 0) {
        loadProgress.value = percent;
      }
      progressFile.textContent = fileLabel(message.file);
      progressValue.textContent =
        message.total > 0
          ? `${percent}% · ${formatBytes(message.loaded)} / ${formatBytes(message.total)}`
          : formatBytes(message.loaded);
      break;
    }
    case "ready":
      isReady = true;
      isEnded = false;
      hasController = message.hasController;
      seedCount = message.seedCount;
      setBackend(message.backend);
      aiAvailability.textContent = hasController ? "Model loaded" : "Not included";
      updateSeedUi(message.seedIndex);
      updatePlaybackUi();
      stage.classList.remove("is-loading");
      stage.setAttribute("aria-busy", "false");
      liveStatus.textContent = `${message.backend === "webgpu" ? "GPU" : "CPU"} runtime ready. Choose when to begin.`;
      showPanel(startPanel);
      break;
    case "frame":
      drawFrame(message);
      break;
    case "playback":
      isPlaying = message.playing;
      aiEnabled = message.aiEnabled;
      updatePlaybackUi();
      break;
    case "warning":
      liveStatus.textContent = message.message;
      break;
    case "error":
      showError(message.message);
      break;
  }
}

worker.addEventListener("message", (event: MessageEvent<WorkerToMainMessage>) => {
  try {
    handleWorkerMessage(event.data);
  } catch (error) {
    showError(error instanceof Error ? error.message : String(error));
  }
});

worker.addEventListener("error", (event) => {
  showError(event.message || "The local inference worker stopped unexpectedly.");
});

startButton.addEventListener("click", startPlayback);
playButton.addEventListener("click", togglePlayback);
stepButton.addEventListener("click", () => {
  if (!isReady || isPlaying || isEnded) {
    return;
  }
  showPanel(null);
  send({ type: "step" });
});
retryButton.addEventListener("click", () => selectSeed(currentSeedIndex, true));
overlayRetry.addEventListener("click", () => selectSeed(currentSeedIndex, true));
overlayNext.addEventListener("click", () => selectSeed(currentSeedIndex + 1, true));
seedPrevious.addEventListener("click", () => selectSeed(currentSeedIndex - 1, false));
seedNext.addEventListener("click", () => selectSeed(currentSeedIndex + 1, false));
reloadButton.addEventListener("click", () => window.location.reload());
aiToggle.addEventListener("click", () => {
  if (!hasController || !isReady) {
    return;
  }
  aiEnabled = !aiEnabled;
  if (aiEnabled) {
    releaseAllInputs();
  }
  updatePlaybackUi();
  send({ type: "set-ai", enabled: aiEnabled });
});

function isControlTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("button, a, input, select, textarea"));
}

function bindPointerHold(target: HTMLElement, prefix: string): void {
  target.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !isReady || isEnded || aiEnabled || isControlTarget(event.target)) {
      return;
    }
    event.preventDefault();
    target.setPointerCapture(event.pointerId);
    setInputHeld(`${prefix}:${event.pointerId}`, true);
  });

  const release = (event: PointerEvent): void => {
    setInputHeld(`${prefix}:${event.pointerId}`, false);
  };
  target.addEventListener("pointerup", release);
  target.addEventListener("pointercancel", release);
  target.addEventListener("lostpointercapture", release);
}

bindPointerHold(stage, "stage");

jumpButton.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || jumpButton.disabled) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  jumpButton.setPointerCapture(event.pointerId);
  setInputHeld(`jump:${event.pointerId}`, true);
});

for (const eventName of ["pointerup", "pointercancel", "lostpointercapture"] as const) {
  jumpButton.addEventListener(eventName, (event) => {
    event.stopPropagation();
    setInputHeld(`jump:${event.pointerId}`, false);
  });
}

window.addEventListener("keydown", (event) => {
  const jumpKey = event.code === "Space" || event.code === "ArrowUp";
  if (jumpKey && !isControlTarget(event.target)) {
    event.preventDefault();
    if (!event.repeat && isReady && !isEnded && !aiEnabled) {
      setInputHeld(`key:${event.code}`, true);
      if (!isPlaying && !isEnded) {
        startPlayback();
      }
    }
    return;
  }

  if (event.repeat || isControlTarget(event.target) || !isReady) {
    return;
  }
  switch (event.code) {
    case "KeyP":
      event.preventDefault();
      togglePlayback();
      break;
    case "Period":
      event.preventDefault();
      if (!isPlaying && !isEnded) {
        showPanel(null);
        send({ type: "step" });
      }
      break;
    case "KeyR":
      event.preventDefault();
      selectSeed(currentSeedIndex, true);
      break;
    case "KeyN":
      event.preventDefault();
      selectSeed(currentSeedIndex + 1, false);
      break;
    case "KeyA":
      if (hasController) {
        event.preventDefault();
        aiToggle.click();
      }
      break;
  }
});

window.addEventListener("keyup", (event) => {
  if (event.code === "Space" || event.code === "ArrowUp") {
    setInputHeld(`key:${event.code}`, false);
  }
});

window.addEventListener("blur", releaseAllInputs);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    releaseAllInputs();
    if (isPlaying) {
      send({ type: "pause" });
    }
  }
});

window.addEventListener("beforeunload", () => worker.terminate());

updateSeedUi(0);
setControlsEnabled(false);
stage.setAttribute("aria-busy", "true");
showPanel(loadingPanel);

const modelOverride = new URLSearchParams(window.location.search).get("models");
const modelBaseUrl = new URL(modelOverride ?? "../static/models/v7/", window.location.href).href;
send({ type: "init", modelBaseUrl });
