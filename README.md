# ComfyUI-pixelscience-nodes

A small set of ComfyUI save/load nodes, derived from
[ComfyUI-Dehypnotic](https://github.com/Dehypnotic/ComfyUI-Dehypnotic) (MIT) and
extended from there.

## Nodes

| Node | Class key | Purpose |
|---|---|---|
| Save Video | `SaveVideoDehypnotic` | ffmpeg-based video writer, optional audio mux |
| Save Images | `SaveImagesDehypnotic` | image writer |
| Save Audio | `SaveAudioMP3Dehypnotic` | MP3 writer |
| FrameSave | `FrameSaveDehypnotic` | frame exporter |
| Load Video | `LoadVideoDehypnotic` | video loader |
| AspectRatio | `dehypnotic_AspectRatio` | aspect ratio calculator |

The class keys intentionally match upstream so existing workflows keep loading.
**Do not rename them** — they are stored inside every saved workflow JSON.
Display names are cosmetic and safe to change.

Because the keys are shared, this pack and the original ComfyUI-Dehypnotic
cannot be installed at the same time; whichever loads last silently wins.

## Changes from upstream

### Save Video

- **`embed_workflow` (default on).** Writes the `workflow` and `prompt` tags into
  the container so the saved file can be dragged back into ComfyUI to restore the
  graph, matching the built-in SaveVideo node. Honours ComfyUI's
  `--disable-metadata` flag.

  Two details make this work: MP4/MOV need `-movflags use_metadata_tags` or the
  muxer silently discards non-standard tags, and the tags are passed via an
  ffmetadata sidecar file because a workflow JSON blob routinely exceeds the
  ~32k character Windows command-line limit. Verified round-tripping in
  mp4, mov, mkv and webm, with and without audio. Note that the Matroska muxer
  uppercases tag keys (`WORKFLOW`), which is also what ComfyUI's own SaveWEBM
  node produces.

- **`[date_subfolder]` token** for `file_path` and `filename_prefix`. Expands to
  the rendered `date_subfolder_pattern`, so the filename can carry exactly the
  same date string as the folder — including custom patterns like `%Y-%m-%d_%H`,
  where the pre-existing `[date]` token would drift. The subfolder is still
  appended independently, so using the token in `file_path` yields the date
  twice unless the pattern is blank.

- **Compact progress.** The encode loop now redraws a single self-overwriting
  line instead of printing ~50 lines. Writes begin with `\r`, which ComfyUI's
  `LogInterceptor` treats as "replace the previous partial line", so the web log
  panel keeps one rolling entry. Falls back to occasional full lines when stdout
  is not a terminal.

  ```
  [SaveVideo] Encoding [########--------]  50.0% 250/500 12.5fps eta 00:20
  ```

## Install

Clone next to your other projects and link it into ComfyUI:

```
git clone <your-remote> N:\AI\ComfyUI-pixelscience-nodes
cmd /c mklink /J "N:\AI\ComfyUI\custom_nodes\ComfyUI-pixelscience-nodes" "N:\AI\ComfyUI-pixelscience-nodes"
```

Requires `imageio-ffmpeg` (bundled ffmpeg), `Pillow`, and `lameenc` for MP3
output. Restart ComfyUI fully after any change — custom node Python is imported
once at startup.

## Save paths

The save nodes write inside `ComfyUI/output` by default. To allow other
locations, put `dehypnotic_save_allowed_paths.json` in `<ComfyUI>/user/config/`:

```json
{ "allowed_roots": ["D:/VideoExports"] }
```

## Credits

Original nodes by [Dehypnotic](https://github.com/Dehypnotic/ComfyUI-Dehypnotic),
MIT licensed. See `LICENSE`.
