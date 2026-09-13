// Verify the CDN upload path for video and audio (not just images).
// Uploading costs nothing — no generation task is created.
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { AvmClient, sniffFile } from './client.mjs';

const c = new AvmClient({ cookie: process.env.AVM_COOKIE });
const sha = (b) => createHash('sha256').update(b).digest('hex').slice(0, 16);

const FILES = ['/tmp/test-video.mp4', '/tmp/test-tone.mp3'];

for (const path of FILES) {
  const buf = fs.readFileSync(path);
  const sn = sniffFile(buf);
  console.log('='.repeat(70));
  console.log(path, '->', sn.kind, sn.contentType, `${(buf.length / 1024).toFixed(1)}KB`, 'sha=' + sha(buf));

  // presign, to read the server-side size cap for this content type
  const pre = await c.trpc('uploads.getPresignedUrl', {
    fileName: `probe.${sn.ext}`, contentType: sn.contentType, fileSize: buf.length, permanent: false,
  }, { method: 'POST' });
  console.log('  maxBytes:', pre.maxBytes, `(${(pre.maxBytes / 1048576).toFixed(0)}MB)`);
  console.log('  publicUrl:', pre.publicUrl);

  // real upload
  const up = await c.uploadFile(buf, { name: path.split('/').pop() });
  console.log('  uploaded:', up.publicUrl);

  // read it back and compare
  const back = await fetch(up.publicUrl);
  const got = Buffer.from(await back.arrayBuffer());
  console.log('  roundtrip http=', back.status, 'content-type=', back.headers.get('content-type'));
  console.log('  bytes match:', got.length === buf.length && sha(got) === sha(buf) ? 'YES ✓' : `NO (got ${got.length})`);
  console.log();
}
