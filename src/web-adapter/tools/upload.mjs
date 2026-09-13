// Upload any local media (image / video / audio) to aivideomaker's CDN.
//
// Discovered in the site's own JS bundle:
//   const {uploadUrl, headers, publicUrl, maxBytes} =
//     await trpc.uploads.getPresignedUrl.mutate({fileName, contentType, fileSize, permanent});
//   PUT uploadUrl with those headers
//
// Why this matters: the site rejects external URLs whose Content-Type is not in
// its allowlist (e.g. the non-standard `image/jpg`).  Re-hosting always yields
// an accepted `static.img2video.ai` URL.
//
// Measured size caps: image 10MB, video 50MB, audio 15MB.
// 参考素材 (omni-reference) needs video and audio, not just images.
//
// Usage:  AVM_COOKIE='...' node upload.mjs <path-or-https-url> [--permanent]

import { readFile } from 'node:fs/promises';
import { AvmClient } from '../client.mjs';

const target = process.argv[2];
if (!target) {
  console.error('usage: node upload.mjs <path-or-https-url> [--permanent]');
  process.exit(1);
}
const permanent = process.argv.includes('--permanent');

const avm = new AvmClient({ cookie: process.env.AVM_COOKIE });
const input = /^https?:\/\//i.test(target) ? target : await readFile(target);
const up = await avm.uploadFile(input, { name: target.split('/').pop(), permanent });

const check = await fetch(up.publicUrl, { method: 'HEAD' }).catch(() => null);

console.log(JSON.stringify({
  ...up,
  permanent,
  servedHttp: check?.status ?? null,
  servedContentType: check?.headers?.get('content-type') ?? null,
}, null, 2));
