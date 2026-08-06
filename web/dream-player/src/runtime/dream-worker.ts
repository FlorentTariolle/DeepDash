/// <reference lib="webworker" />

// The base 1.26 entrypoint is the JSEP build that powers the WebGPU EP.
// The narrower `/webgpu` entrypoint uses the separate Asyncify runtime.
import * as ort from "onnxruntime-web";
import jsepWasmUrl from "onnxruntime-web/ort-wasm-simd-threaded.jsep.wasm?url";

import type {
  MainToWorkerMessage,
  RuntimeBackend,
  WorkerToMainMessage,
} from "./protocol";
import { parseSeedBundle, type DreamSeed, type SeedBundle } from "./seeds";

const workerScope = self as unknown as DedicatedWorkerGlobalScope;

const CONTEXT_FRAMES = 4;
const BLOCK_SIZE = 65;
const VISUAL_TOKENS = 64;
const VOCAB_SIZE = 1000;
const ALIVE_TOKEN = 1000;
const DEATH_TOKEN = 1001;
const GRID_SIZE = 8;
const MAX_DREAM_STEPS = 45;
const TARGET_FRAME_MS = 1000 / 30;
const FSQ_DIVISORS = [125, 25, 5, 1] as const;
const FSQ_HALF_LEVELS = [4, 2, 2, 2] as const;

interface ModelBytes {
  world: Uint8Array;
  decoder: Uint8Array;
  controller?: Uint8Array;
}

interface RuntimeSessions {
  world: ort.InferenceSession;
  decoder: ort.InferenceSession;
  controller?: ort.InferenceSession;
}

interface WorldResult {
  predictedTokens: Int32Array;
  deathProbability: number;
  hiddenState: Float32Array;
}

let modelBaseUrl = "";
let seedBundle: SeedBundle | undefined;
let sessions: RuntimeSessions | undefined;
let backend: RuntimeBackend = "wasm";
let currentSeedIndex = 0;
let frameTokens = new Int32Array(CONTEXT_FRAMES * BLOCK_SIZE);
let actions = new Int32Array(CONTEXT_FRAMES);
let stepCount = 0;
let heldAction: 0 | 1 = 0;
let aiEnabled = false;
let playing = false;
let ended = false;
let loopGeneration = 0;
let operationQueue: Promise<void> = Promise.resolve();

function post(message: WorkerToMainMessage, transfer: Transferable[] = []): void {
  workerScope.postMessage(message, transfer);
}

function describeError(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function assetUrl(fileName: string): string {
  return new URL(fileName, modelBaseUrl).href;
}

async function fetchBytes(fileName: string, required: boolean): Promise<Uint8Array | undefined> {
  const response = await fetch(assetUrl(fileName), { cache: "force-cache" });
  if (!response.ok) {
    if (!required && response.status === 404) {
      return undefined;
    }
    throw new Error(`Could not download ${fileName} (HTTP ${response.status}).`);
  }

  const contentLength = Number(response.headers.get("content-length") ?? 0);
  const reader = response.body?.getReader();
  if (!reader) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    post({ type: "progress", file: fileName, loaded: bytes.byteLength, total: bytes.byteLength });
    return bytes;
  }

  const chunks: Uint8Array[] = [];
  let loaded = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    chunks.push(value);
    loaded += value.byteLength;
    post({ type: "progress", file: fileName, loaded, total: contentLength });
  }

  const bytes = new Uint8Array(loaded);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  post({ type: "progress", file: fileName, loaded, total: contentLength || loaded });
  return bytes;
}

async function downloadAssets(): Promise<{ models: ModelBytes; seeds: ArrayBuffer }> {
  post({
    type: "status",
    phase: "downloading",
    title: "Downloading the world model",
    detail: "The model is cached by your browser after the first visit.",
  });

  const world = await fetchBytes("world.onnx", true);
  const decoder = await fetchBytes("decoder.onnx", true);
  const seedBytes = await fetchBytes("seeds.bin", true);
  const controller = await fetchBytes("controller.onnx", false);

  if (!world || !decoder || !seedBytes) {
    throw new Error("A required dream-player asset is missing.");
  }

  return {
    models: { world, decoder, ...(controller ? { controller } : {}) },
    seeds: seedBytes.buffer.slice(
      seedBytes.byteOffset,
      seedBytes.byteOffset + seedBytes.byteLength,
    ) as ArrayBuffer,
  };
}

type ExecutionProviders = NonNullable<
  ort.InferenceSession.SessionOptions["executionProviders"]
>;

function sessionOptions(executionProviders: ExecutionProviders): ort.InferenceSession.SessionOptions {
  return {
    executionProviders,
    graphOptimizationLevel: "all",
    enableCpuMemArena: true,
    enableMemPattern: true,
  };
}

async function createCoreSessions(
  models: ModelBytes,
  executionProviders: ExecutionProviders,
): Promise<RuntimeSessions> {
  const options = sessionOptions(executionProviders);
  const world = await ort.InferenceSession.create(models.world, options);
  try {
    const decoder = await ort.InferenceSession.create(models.decoder, options);
    return { world, decoder };
  } catch (error) {
    await world.release();
    throw error;
  }
}

async function requireWebGpuAdapter(): Promise<void> {
  const navigatorWithGpu = workerScope.navigator as Navigator & {
    gpu?: {
      requestAdapter(options?: { powerPreference?: "low-power" | "high-performance" }): Promise<unknown | null>;
    };
  };
  if (!navigatorWithGpu.gpu) {
    throw new Error("This browser does not expose WebGPU.");
  }
  const adapter = await navigatorWithGpu.gpu.requestAdapter({
    powerPreference: "high-performance",
  });
  if (!adapter) {
    throw new Error("No compatible WebGPU adapter was found.");
  }
}

async function createSessions(models: ModelBytes): Promise<RuntimeSessions> {
  post({
    type: "status",
    phase: "initializing",
    title: "Starting WebGPU",
    detail: "Compiling the model for your graphics device.",
  });

  let core: RuntimeSessions;
  try {
    await requireWebGpuAdapter();
    // Keep WASM available for shape/metadata operators that WebGPU cannot claim.
    // A total WebGPU adapter or session failure is caught below and retried CPU-only.
    core = await createCoreSessions(models, ["webgpu", "wasm"]);
    backend = "webgpu";
  } catch (webGpuError) {
    post({
      type: "warning",
      message: `WebGPU was unavailable (${describeError(webGpuError)}). Retrying with WebAssembly.`,
    });
    post({
      type: "status",
      phase: "fallback",
      title: "Switching to WebAssembly",
      detail: "The dream will still run locally, with a slower CPU backend.",
    });
    core = await createCoreSessions(models, ["wasm"]);
    backend = "wasm";
  }

  if (models.controller) {
    try {
      core.controller = await ort.InferenceSession.create(
        models.controller,
        sessionOptions(backend === "webgpu" ? ["webgpu", "wasm"] : ["wasm"]),
      );
    } catch (controllerError) {
      post({
        type: "warning",
        message: `AI pilot disabled: ${describeError(controllerError)}`,
      });
    }
  }

  return core;
}

function copySeed(seed: DreamSeed): void {
  frameTokens = new Int32Array(seed.frameTokens);
  actions = new Int32Array(seed.actions);
  stepCount = 0;
  ended = false;
  heldAction = 0;
}

function tokensToCodes(tokens: Int32Array): Float32Array {
  if (tokens.length !== VISUAL_TOKENS) {
    throw new Error(`Expected ${VISUAL_TOKENS} visual tokens, received ${tokens.length}.`);
  }

  const codes = new Float32Array(4 * VISUAL_TOKENS);
  for (let position = 0; position < VISUAL_TOKENS; position += 1) {
    const token = tokens[position];
    if (token === undefined || token < 0 || token >= VOCAB_SIZE) {
      throw new Error(`Invalid visual token ${String(token)} at position ${position}.`);
    }

    let remainder = token;
    for (let dimension = 0; dimension < FSQ_DIVISORS.length; dimension += 1) {
      const divisor = FSQ_DIVISORS[dimension];
      const halfLevel = FSQ_HALF_LEVELS[dimension];
      if (divisor === undefined || halfLevel === undefined) {
        throw new Error("Invalid FSQ conversion constants.");
      }
      const digit = Math.floor(remainder / divisor);
      remainder %= divisor;
      codes[dimension * VISUAL_TOKENS + position] = digit - halfLevel;
    }
  }
  return codes;
}

function visualTokensFromLastFrame(): Int32Array {
  const offset = (CONTEXT_FRAMES - 1) * BLOCK_SIZE;
  return new Int32Array(frameTokens.slice(offset, offset + VISUAL_TOKENS));
}

function outputTensor(
  outputs: ort.InferenceSession.OnnxValueMapType,
  name: string,
): ort.Tensor {
  const value = outputs[name];
  if (!(value instanceof ort.Tensor)) {
    throw new Error(`Model output '${name}' is missing or is not a tensor.`);
  }
  return value;
}

function disposeTensor(tensor: ort.Tensor | undefined): void {
  tensor?.dispose();
}

async function decodeFrame(tokens: Int32Array): Promise<Uint8ClampedArray> {
  if (!sessions) {
    throw new Error("Decoder session is not ready.");
  }

  const codes = tokensToCodes(tokens);
  const feeds = {
    codes: new ort.Tensor("float32", codes, [1, 4, GRID_SIZE, GRID_SIZE]),
  };
  const outputs = await sessions.decoder.run(feeds);
  const frameTensor = outputTensor(outputs, "frame");

  try {
    const values = frameTensor.data as Float32Array;
    if (values.length !== GRID_SIZE * GRID_SIZE * 64) {
      throw new Error(`Decoder returned ${values.length} pixels; expected 4096.`);
    }
    const pixels = new Uint8ClampedArray(values.length * 4);
    for (let index = 0; index < values.length; index += 1) {
      const value = values[index] ?? 0;
      const shade = Math.round(Math.min(1, Math.max(0, value)) * 255);
      const pixelOffset = index * 4;
      pixels[pixelOffset] = shade;
      pixels[pixelOffset + 1] = shade;
      pixels[pixelOffset + 2] = shade;
      pixels[pixelOffset + 3] = 255;
    }
    return pixels;
  } finally {
    disposeTensor(frameTensor);
  }
}

function argmaxVisualTokens(logits: Float32Array): Int32Array {
  const expected = BLOCK_SIZE * (VOCAB_SIZE + 2);
  if (logits.length !== expected) {
    throw new Error(`World model returned ${logits.length} logits; expected ${expected}.`);
  }

  const output = new Int32Array(VISUAL_TOKENS);
  const fullVocabSize = VOCAB_SIZE + 2;
  for (let position = 0; position < VISUAL_TOKENS; position += 1) {
    const rowOffset = position * fullVocabSize;
    let bestIndex = 0;
    let bestValue = logits[rowOffset] ?? Number.NEGATIVE_INFINITY;
    for (let token = 1; token < VOCAB_SIZE; token += 1) {
      const value = logits[rowOffset + token] ?? Number.NEGATIVE_INFINITY;
      if (value > bestValue) {
        bestIndex = token;
        bestValue = value;
      }
    }
    output[position] = bestIndex;
  }
  return output;
}

function deathProbability(logits: Float32Array): number {
  const fullVocabSize = VOCAB_SIZE + 2;
  const statusOffset = VISUAL_TOKENS * fullVocabSize;
  const aliveLogit = logits[statusOffset + ALIVE_TOKEN] ?? Number.NEGATIVE_INFINITY;
  const deathLogit = logits[statusOffset + DEATH_TOKEN] ?? Number.NEGATIVE_INFINITY;
  const maximum = Math.max(aliveLogit, deathLogit);
  const aliveExp = Math.exp(aliveLogit - maximum);
  const deathExp = Math.exp(deathLogit - maximum);
  return deathExp / (aliveExp + deathExp);
}

async function runWorld(): Promise<WorldResult> {
  if (!sessions) {
    throw new Error("World-model session is not ready.");
  }

  const feeds = {
    frame_tokens: new ort.Tensor("int32", frameTokens, [1, CONTEXT_FRAMES, BLOCK_SIZE]),
    actions: new ort.Tensor("int32", actions, [1, CONTEXT_FRAMES]),
  };
  const outputs = await sessions.world.run(feeds);
  const logitsTensor = outputTensor(outputs, "logits");
  const hiddenTensor = outputTensor(outputs, "h_t");

  try {
    const logits = logitsTensor.data as Float32Array;
    const hidden = hiddenTensor.data as Float32Array;
    if (hidden.length !== 384) {
      throw new Error(`World model returned h_t length ${hidden.length}; expected 384.`);
    }
    return {
      predictedTokens: argmaxVisualTokens(logits),
      deathProbability: deathProbability(logits),
      hiddenState: new Float32Array(hidden),
    };
  } finally {
    disposeTensor(logitsTensor);
    disposeTensor(hiddenTensor);
  }
}

async function runController(
  tokenIds: Int32Array,
  hiddenState: Float32Array,
): Promise<0 | 1> {
  if (!sessions?.controller) {
    return heldAction;
  }

  const feeds = {
    token_ids: new ort.Tensor("int32", tokenIds, [1, VISUAL_TOKENS]),
    h_t: new ort.Tensor("float32", hiddenState, [1, 384]),
  };
  const outputs = await sessions.controller.run(feeds);
  const actionTensor = outputTensor(outputs, "action_prob");
  try {
    const probability = Number(actionTensor.data[0] ?? 0);
    return probability > 0.5 ? 1 : 0;
  } finally {
    disposeTensor(actionTensor);
  }
}

function shiftContext(predictedTokens: Int32Array, action: 0 | 1): void {
  frameTokens.copyWithin(0, BLOCK_SIZE);
  const frameOffset = (CONTEXT_FRAMES - 1) * BLOCK_SIZE;
  frameTokens.set(predictedTokens, frameOffset);
  frameTokens[frameOffset + VISUAL_TOKENS] = ALIVE_TOKEN;

  actions.copyWithin(0, 1);
  actions[CONTEXT_FRAMES - 1] = action;
}

function postPixels(
  pixels: Uint8ClampedArray,
  action: 0 | 1,
  probability: number,
  latencyMs: number,
  endReason?: "death" | "limit",
): void {
  const transferable = pixels.buffer as ArrayBuffer;
  post(
    {
      type: "frame",
      pixels: transferable,
      width: 64,
      height: 64,
      seedIndex: currentSeedIndex,
      step: stepCount,
      action,
      deathProbability: probability,
      latencyMs,
      ended: endReason !== undefined,
      ...(endReason ? { endReason } : {}),
    },
    [transferable],
  );
}

async function emitSeedFrame(): Promise<void> {
  const pixels = await decodeFrame(visualTokensFromLastFrame());
  postPixels(pixels, 0, 0, 0);
}

async function resetRun(seedIndex: number, autoplay: boolean): Promise<void> {
  if (!seedBundle || !sessions) {
    return;
  }
  const normalized = ((seedIndex % seedBundle.seeds.length) + seedBundle.seeds.length) %
    seedBundle.seeds.length;
  const seed = seedBundle.seeds[normalized];
  if (!seed) {
    throw new Error(`Dream seed ${normalized + 1} is unavailable.`);
  }

  loopGeneration += 1;
  playing = false;
  aiEnabled = aiEnabled && Boolean(sessions.controller);
  currentSeedIndex = normalized;
  copySeed(seed);
  await emitSeedFrame();
  post({ type: "playback", playing: false, aiEnabled });

  if (autoplay) {
    startPlaying();
  } else {
    post({
      type: "status",
      phase: "ready",
      title: "World model ready",
      detail: "Hold to jump, then start the rollout.",
    });
  }
}

function scheduleNextTick(generation: number, elapsedMs: number): void {
  const delay = Math.max(0, TARGET_FRAME_MS - elapsedMs);
  workerScope.setTimeout(() => {
    enqueue(async () => {
      if (playing && generation === loopGeneration) {
        await generateStep(generation);
      }
    });
  }, delay);
}

async function generateStep(generation: number): Promise<void> {
  if (!sessions || ended || generation !== loopGeneration) {
    return;
  }

  const startedAt = performance.now();

  // Native timing: capture the human action for the context update, but predict
  // this frame from the existing context. The captured action conditions the
  // following world-model tick after the context window shifts.
  let actionForNextContext: 0 | 1 = heldAction;
  const currentVisualTokens = visualTokensFromLastFrame();
  const world = await runWorld();

  if (aiEnabled && sessions.controller) {
    actionForNextContext = await runController(currentVisualTokens, world.hiddenState);
  }

  const pixels = await decodeFrame(world.predictedTokens);
  stepCount += 1;

  const died = world.deathProbability > 0.5;
  const reachedLimit = stepCount >= MAX_DREAM_STEPS;
  const endReason = died ? "death" : reachedLimit ? "limit" : undefined;
  const latencyMs = performance.now() - startedAt;

  if (endReason) {
    ended = true;
    playing = false;
    loopGeneration += 1;
  } else {
    shiftContext(world.predictedTokens, actionForNextContext);
  }

  postPixels(
    pixels,
    actionForNextContext,
    world.deathProbability,
    latencyMs,
    endReason,
  );

  if (endReason) {
    post({ type: "playback", playing: false, aiEnabled });
    post({
      type: "status",
      phase: "ended",
      title: endReason === "death" ? "Dream over" : "Horizon reached",
      detail:
        endReason === "death"
          ? "The model predicted a collision."
          : `This rollout reached its ${MAX_DREAM_STEPS}-step horizon.`,
    });
  } else if (playing && generation === loopGeneration) {
    scheduleNextTick(generation, latencyMs);
  }
}

function startPlaying(): void {
  if (!sessions || ended || playing) {
    return;
  }
  playing = true;
  loopGeneration += 1;
  const generation = loopGeneration;
  post({ type: "playback", playing: true, aiEnabled });
  post({
    type: "status",
    phase: "playing",
    title: "Dream in progress",
    detail: aiEnabled ? "The AI pilot is choosing each action." : "Hold to jump. Release to idle.",
  });
  enqueue(() => generateStep(generation));
}

function pausePlaying(): void {
  if (!playing) {
    return;
  }
  playing = false;
  loopGeneration += 1;
  post({ type: "playback", playing: false, aiEnabled });
  post({
    type: "status",
    phase: "paused",
    title: "Rollout paused",
    detail: "Step once or resume when you are ready.",
  });
}

async function warmup(): Promise<void> {
  if (!sessions) {
    return;
  }
  post({
    type: "status",
    phase: "warmup",
    title: "Warming up the dream",
    detail: "Running the first local inference pass.",
  });
  const world = await runWorld();
  await decodeFrame(world.predictedTokens);
  if (sessions.controller) {
    await runController(visualTokensFromLastFrame(), world.hiddenState);
  }
}

async function initialize(baseUrl: string): Promise<void> {
  modelBaseUrl = new URL(baseUrl).href;
  ort.env.logLevel = "warning";
  ort.env.wasm.numThreads = workerScope.crossOriginIsolated ? 0 : 1;
  ort.env.wasm.proxy = false;
  ort.env.wasm.wasmPaths = {
    wasm: new URL(jsepWasmUrl, workerScope.location.href).href,
  };

  post({
    type: "status",
    phase: "capability",
    title: "Checking your browser",
    detail: "Looking for WebGPU and a compatible local runtime.",
  });

  const assets = await downloadAssets();
  seedBundle = parseSeedBundle(assets.seeds);
  const firstSeed = seedBundle.seeds[0];
  if (!firstSeed) {
    throw new Error("The seed bundle is empty.");
  }
  copySeed(firstSeed);

  sessions = await createSessions(assets.models);
  await warmup();
  await resetRun(0, false);

  post({
    type: "ready",
    backend,
    hasController: Boolean(sessions.controller),
    seeds: seedBundle.seeds.map(({ index, name, description }) => ({
      index,
      name,
      description,
    })),
    seedIndex: currentSeedIndex,
  });
}

function enqueue(operation: () => Promise<void>): void {
  operationQueue = operationQueue.then(operation).catch((error: unknown) => {
    playing = false;
    loopGeneration += 1;
    post({ type: "error", message: describeError(error) });
  });
}

workerScope.addEventListener("message", (event: MessageEvent<MainToWorkerMessage>) => {
  const message = event.data;
  switch (message.type) {
    case "init":
      enqueue(() => initialize(message.modelBaseUrl));
      break;
    case "set-action":
      heldAction = message.held ? 1 : 0;
      break;
    case "play":
      startPlaying();
      break;
    case "pause":
      pausePlaying();
      break;
    case "step": {
      if (!playing && !ended && sessions) {
        const generation = loopGeneration;
        enqueue(() => generateStep(generation));
      }
      break;
    }
    case "reset":
      enqueue(() => resetRun(message.seedIndex, message.autoplay));
      break;
    case "set-ai":
      aiEnabled = message.enabled && Boolean(sessions?.controller);
      post({ type: "playback", playing, aiEnabled });
      break;
  }
});
