// Parity runner: executes the web app's worker parser under Node against a
// fixture file and prints {entities, edges} as JSON.
//
//   node run_js.mjs <path-to-web/index.html> <chunkSize> <fixture.xml> [more.xml...]
//
// The worker source is extracted from the <script id="worker-src"> block of
// web/index.html — that tag is the extraction API; renaming it breaks CI.

import { readFileSync } from 'node:fs';

const [, , webApp, chunkArg, ...fixtures] = process.argv;
const CHUNK = parseInt(chunkArg || '65536', 10);

const html = readFileSync(webApp, 'utf-8');
const m = html.match(/<script id="worker-src" type="text\/js-worker">([\s\S]*?)<\/script>/);
if (!m) { console.error('worker-src block not found in ' + webApp); process.exit(2); }
const workerSrc = m[1];

// Worker-global shims. postMessage collects; done/error settle the promise.
let settle, reject;
const finished = new Promise((res, rej) => { settle = res; reject = rej; });
globalThis.onmessage = null;
globalThis.postMessage = (msg) => {
  if (msg.type === 'done') settle(msg.data);
  else if (msg.type === 'error') reject(new Error(msg.message));
};

(0, eval)(workerSrc); // defines onmessage handler in worker style

// Fake Files: bytes fed in fixed-size chunks to stress the incremental tokenizer.
const fakeFiles = fixtures.map((fixture) => {
  const bytes = new Uint8Array(readFileSync(fixture));
  return {
    name: fixture.split('/').pop(),
    size: bytes.length,
    stream() {
      let off = 0;
      return new ReadableStream({
        pull(c) {
          if (off >= bytes.length) { c.close(); return; }
          c.enqueue(bytes.subarray(off, Math.min(off + CHUNK, bytes.length)));
          off += CHUNK;
        },
      });
    },
  };
});

globalThis.onmessage({ data: { files: fakeFiles } });

const data = await finished;
process.stdout.write(JSON.stringify({ entities: data.entities, edges: data.edges }));
