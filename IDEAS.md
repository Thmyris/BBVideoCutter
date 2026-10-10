# Ideas

Not planned yet; notes for later.

## Lossless crop

### Header crop (H.264 / HEVC) — lossless, no size reduction

Rewrite the crop window the codec already has in its header (H.264 SPS
`frame_cropping`, HEVC SPS conformance window) with ffmpeg's
`h264_metadata` / `hevc_metadata` bitstream filters during a `-c copy`:

```
ffmpeg -i in.mp4 -map 0 -c copy -bsf:v h264_metadata=crop_left=L:crop_right=R:crop_top=T:crop_bottom=B out.mp4
```

Tested on 2026-10-10 (ffmpeg 9.0.1, the app's libmpv-2.dll):

- In MP4 the video packets stay byte-identical to the source; only the
  header changes. Runs at copy speed. File size doesn't shrink (the
  hidden pixels are still stored).
- ffmpeg decodes pixel-exactly the same frames as a real crop, any even
  crop, H.264 and HEVC.
- The values replace the stream's own crop instead of adding to it: 360p
  H.264 is coded as 368 with crop_bottom 8, so that 8 must be added or the
  result is 8 px too tall.
- mpv shows top/right/bottom exactly, but the left crop only in steps of
  64 px (smaller left crops are dropped; libavcodec keeps the data pointers
  aligned). Snap the left edge to 64 px, or accept it.
- Not checked: VLC, Windows' player, browsers, phone hardware decoders.
- Other codecs (AV1, VP9, ...) have no such crop. Container-level crop
  (MKV `PixelCrop`, MP4 `clap`) works for any codec but player support
  looks thin; untested.
- Plan if built: a "Lossless crop" mode on the Crop/Rotate tab for
  H.264/HEVC, falling back to the re-encode for other codecs. Optional
  extra: a truly lossless re-encode (x264 `-qp 0` / FFV1) for any codec,
  at several times the file size.

### Compressed-domain crop — lossless *and* smaller (research idea)

Keep the bits of the blocks that are inside the crop and rebuild the rest,
so a crop to half the picture gives roughly the kept region's share of the
bytes, still byte-exact on decode.

Why the bytes can't just be cut apart:

1. CABAC entropy coding: every bit depends on everything before it in the
   slice and on neighbouring blocks' contexts. Slice data has to be fully
   parsed to syntax elements (lossless) and written again.
2. Intra prediction of edge blocks used pixels that are cropped away.
3. Motion vectors can point into the removed area of reference frames;
   MV prediction / merge candidates (also temporal ones) depend on
   neighbours that may be gone.
4. Deblocking (and SAO in HEVC) filtered edge pixels using removed pixels.

How it could work:

- Re-code kept blocks' motion data for the new layout (cheap).
- Blocks whose decode changes (the edge band, blocks with MVs leaving the
  picture) are coded losslessly against the original decoded pixels:
  HEVC `cu_transquant_bypass` per CU (loop filters leave those samples
  alone, as I recall the spec); H.264 only has I_PCM (384 bytes per
  16x16 MB, too expensive).
- Cut only on the block grid (HEVC CTU, usually 64 px; H.264 16 px) so the
  partitioning doesn't shift, then do the exact pixel edge with the header
  crop above.

Expected size: the share of bits the kept region actually used (not half
the area: bits follow detail and motion) plus the correction overhead.
Rough guess for 1080p30, vertical cut: an 8 px lossless band is about
1 Mbit/s + chroma + out-of-picture-MV blocks (grows with panning);
roughly "kept share + 10-20 %". With H.264 I_PCM the band alone is about
6 Mbit/s.

Effort: needs an own CABAC decoder/encoder for slice data, MV candidate
rebuilding and a model of intra prediction and loop filters — a large
part of an encoder. ffmpeg's CBS only handles headers. No open-source
tool known that does this losslessly (not verified).

Cheap special case: HEVC encoded with motion-constrained tiles (MCTS) can
be cut along tile edges almost for free (used for 360° video); normal
recorders don't encode that way.

First step if pursued: measure how many bytes each region of a real
recording uses (bits per block) to see what an ideal crop would give.
