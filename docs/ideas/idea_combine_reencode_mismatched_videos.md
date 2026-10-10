# Combine: re-encode mismatched videos instead of failing

Today Combine fails with an ffmpeg error when the videos differ in codec,
size, frame rate and so on. Instead it should re-encode them to match, and
tell the user it did.

Design (built once on 2026-10-10 and then removed, not wanted yet):

- Probe each file once with ffprobe (json): video codec, displayed size
  after rotation, pix_fmt, frame rate, rotation, audio codec, sample rate,
  channels, duration. Compare them and name what differs ("video codec",
  "resolution", "frame rate", "rotation", "audio", ...).
- A live note under the Combine list (theme-aware warning color) when the
  current list will be re-encoded: "These videos differ (...), so they
  will be re-encoded to match the first one."
- Matching files: the existing lossless TS route, unchanged. If ffmpeg
  still can't stream-copy them, fall back to the re-encode with that as
  the reason.
- Re-encode: one ffmpeg run, shaped like the first file. Per input
  `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H` (black bars),
  `setsar=1,fps=<first>,format=yuv420p`; audio `aresample` + `aformat` to
  the first audio file's rate and layout; `anullsrc` + `atrim` for clips
  without audio; then the `concat` filter. H.264 via the GPU encoder,
  falling back to libx264 once if the GPU one fails. AAC 192k.
  `+faststart` for MP4/MOV.
- `.webm` output can't hold H.264 + AAC, so it is written as `.mp4`.
- The done message says why it was re-encoded, the format it used, and
  "This is not lossless." The status line says "Saved (re-encoded): ...".

Tested then (hidden test, light and dark): matching files stay lossless
(decoded frames identical to the sources); a mix of H.264 640x360 30 fps,
HEVC 320x240, a rotated clip without audio and HEVC 1080p60 came out as
H.264 640x360 30 fps with the rotated clip pillarboxed and audio as long
as the video.
