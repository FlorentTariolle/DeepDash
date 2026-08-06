import type { SeedSummary } from "./protocol";

const MAGIC = "DVMCSEED";
const HEADER_BYTES = 24;
const EXPECTED_VERSION = 1;
const EXPECTED_CONTEXT_FRAMES = 4;
const EXPECTED_BLOCK_SIZE = 65;

const SEED_METADATA = [
  {
    name: "First Light",
    description: "A clean opening with room to learn.",
  },
  {
    name: "The Gauntlet",
    description: "Tight geometry and unforgiving timing.",
  },
  {
    name: "Inverted Flight",
    description: "A transformed section with shifting height.",
  },
  {
    name: "Last Chance",
    description: "A late-run recovery on unstable ground.",
  },
] as const;

export interface DreamSeed extends SeedSummary {
  frameTokens: Int32Array;
  actions: Int32Array;
}

export interface SeedBundle {
  version: number;
  contextFrames: number;
  blockSize: number;
  seeds: DreamSeed[];
}

function requireBytes(buffer: ArrayBuffer, minimum: number, label: string): void {
  if (buffer.byteLength < minimum) {
    throw new Error(`${label} is truncated (${buffer.byteLength} bytes).`);
  }
}

export function parseSeedBundle(buffer: ArrayBuffer): SeedBundle {
  requireBytes(buffer, HEADER_BYTES, "seeds.bin header");

  const magic = new TextDecoder("ascii").decode(new Uint8Array(buffer, 0, 8));
  if (magic !== MAGIC) {
    throw new Error(`Invalid seeds.bin magic: expected ${MAGIC}.`);
  }

  const view = new DataView(buffer);
  const version = view.getUint32(8, true);
  const count = view.getUint32(12, true);
  const contextFrames = view.getUint32(16, true);
  const blockSize = view.getUint32(20, true);

  if (version !== EXPECTED_VERSION) {
    throw new Error(`Unsupported seeds.bin version ${version}.`);
  }
  if (count !== SEED_METADATA.length) {
    throw new Error(
      `Expected ${SEED_METADATA.length} dream seeds, received ${count}.`,
    );
  }
  if (contextFrames !== EXPECTED_CONTEXT_FRAMES || blockSize !== EXPECTED_BLOCK_SIZE) {
    throw new Error(
      `Expected seed shape [${EXPECTED_CONTEXT_FRAMES}, ${EXPECTED_BLOCK_SIZE}], received [${contextFrames}, ${blockSize}].`,
    );
  }

  const tokensPerSeed = contextFrames * blockSize;
  const tokenCount = count * tokensPerSeed;
  const actionCount = count * contextFrames;
  const expectedBytes = HEADER_BYTES + tokenCount * Uint16Array.BYTES_PER_ELEMENT + actionCount;
  requireBytes(buffer, expectedBytes, "seeds.bin payload");

  if (buffer.byteLength !== expectedBytes) {
    throw new Error(
      `Unexpected seeds.bin length: expected ${expectedBytes}, received ${buffer.byteLength}.`,
    );
  }

  const tokenOffset = HEADER_BYTES;
  const actionOffset = tokenOffset + tokenCount * Uint16Array.BYTES_PER_ELEMENT;
  const seeds: DreamSeed[] = [];

  for (let seedIndex = 0; seedIndex < count; seedIndex += 1) {
    const frameTokens = new Int32Array(tokensPerSeed);
    const seedTokenOffset = tokenOffset + seedIndex * tokensPerSeed * Uint16Array.BYTES_PER_ELEMENT;
    for (let index = 0; index < tokensPerSeed; index += 1) {
      frameTokens[index] = view.getUint16(
        seedTokenOffset + index * Uint16Array.BYTES_PER_ELEMENT,
        true,
      );
    }

    const actions = new Int32Array(contextFrames);
    const seedActionOffset = actionOffset + seedIndex * contextFrames;
    for (let index = 0; index < contextFrames; index += 1) {
      const action = view.getUint8(seedActionOffset + index);
      if (action > 1) {
        throw new Error(`Seed ${seedIndex + 1} contains invalid action ${action}.`);
      }
      actions[index] = action;
    }

    const metadata = SEED_METADATA[seedIndex];
    if (!metadata) {
      throw new Error(`Missing metadata for seed ${seedIndex + 1}.`);
    }
    seeds.push({
      index: seedIndex,
      name: metadata.name,
      description: metadata.description,
      frameTokens,
      actions,
    });
  }

  return { version, contextFrames, blockSize, seeds };
}
