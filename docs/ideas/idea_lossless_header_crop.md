# Lossless header crop (H.264 / HEVC)

Lossless, but the file doesn't get smaller.

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

Plan if built: a "Lossless crop" mode on the Crop/Rotate tab for
H.264/HEVC, falling back to the re-encode for other codecs. Optional
extra: a truly lossless re-encode (x264 `-qp 0` / FFV1) for any codec,
at several times the file size.

See also `idea_compressed_domain_crop.md` for a crop that is lossless and
smaller.
