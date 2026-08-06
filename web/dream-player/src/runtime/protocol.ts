export type RuntimeBackend = "webgpu" | "wasm";

export type LoadingPhase =
  | "capability"
  | "downloading"
  | "initializing"
  | "fallback"
  | "warmup"
  | "ready"
  | "playing"
  | "paused"
  | "ended";

export interface SeedSummary {
  index: number;
  name: string;
  description: string;
}

export type MainToWorkerMessage =
  | {
      type: "init";
      modelBaseUrl: string;
    }
  | {
      type: "set-action";
      held: boolean;
    }
  | {
      type: "play";
    }
  | {
      type: "pause";
    }
  | {
      type: "step";
    }
  | {
      type: "reset";
      seedIndex: number;
      autoplay: boolean;
    }
  | {
      type: "set-ai";
      enabled: boolean;
    };

export type WorkerToMainMessage =
  | {
      type: "status";
      phase: LoadingPhase;
      title: string;
      detail: string;
    }
  | {
      type: "progress";
      file: string;
      loaded: number;
      total: number;
    }
  | {
      type: "ready";
      backend: RuntimeBackend;
      hasController: boolean;
      seeds: SeedSummary[];
      seedIndex: number;
    }
  | {
      type: "frame";
      pixels: ArrayBuffer;
      width: number;
      height: number;
      seedIndex: number;
      step: number;
      action: 0 | 1;
      deathProbability: number;
      latencyMs: number;
      ended: boolean;
      endReason?: "death" | "limit";
    }
  | {
      type: "playback";
      playing: boolean;
      aiEnabled: boolean;
    }
  | {
      type: "warning";
      message: string;
    }
  | {
      type: "error";
      message: string;
    };
