# Compressed-domain crop (research idea)

A crop that is lossless *and* smaller. Keep the bits of the blocks that are
inside the crop and rebuild the rest, so a crop to half the picture gives
roughly the kept region's share of the bytes, still byte-exact on decode.

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
  crop (`idea_lossless_header_crop.md`).

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
