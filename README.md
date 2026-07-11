# VRAM / RAM Adjuster — WanGP Plugin

**Adjust the Gpu Vram and System Ram - Override the Memory Profiles**

A WanGP (Wan2GP) plugin that adds a top tab (**"Vram/Ram Adj."**) giving you
fine, manual control over how much **VRAM** and **System RAM** WanGP uses,
instead of only the predefined Memory Profiles (1, 2, 3, 3+, 4, 4+, 5).

## Install

1. In WanGP open the **Plugins** tab → install from URL (or drop the
   `vram_ram_adj` folder into `Wan2GP/plugins/`).
2. Enable the plugin in the Plugins tab and **restart WanGP**.

The plugin folder must be named `vram_ram_adj`.

## The tab

- Title **VRAM / RAM Adjuster** + subtitle.
- **Activate the plugin** — OFF at every launch. When ON, applies the manual
  values below; when turned OFF again it restores WanGP's original settings.
- **GPU VRAM - Gb** slider — 6 → 32, step 0.5, default 8.
- **SYSTEM RAM - Gb** slider — 16 → 128, step 2, default 32.
- **Do not use Reserved Memory** — OFF at every launch. When ON, RAM is used
  like Memory Profile **3+** (no reserved / shared memory).
- **Save / Load** — dropdown of presets + Save / Load / Delete. Presets are
  `.txt` files in the plugin's `Saves/` folder.
- **Auto-unload models when a parameter changes** — when ticked (and the plugin
  is active), models are unloaded automatically as soon as you change a value
  or load a preset, so the next generation reloads them with the new settings.
- **Force Unload Models From RAM** (bottom button) — same action as
  Configuration → Performance. Unloads the models so they reload with the
  current settings on the next generation. Blocked while a GPU process is
  running.

## How it actually drives the memory (verified against wgp.py)

WanGP loads a model through:

```
load_models(model_type, override_profile, output_type, ...)
    profile      = compute_profile(override_profile, output_type)
    mmgp_profile = init_pipe(pipe, kwargs, profile)
        # preload = int(args.preload)
        # if preload == 0: preload = server_config.get("preload_in_VRAM", 0)
        #   -> preload (MB) becomes the VRAM budget of 'transformer' (profiles 2/4/5)
    perc_reserved_mem_max = args.perc_reserved_mem_max
    offload.profile(pipe, profile_no=mmgp_profile,
                    perc_reserved_mem_max=perc_reserved_mem_max, ...)
```

So the two real levers are:

| Control (this plugin)        | Real WanGP variable                | Unit / meaning                         |
|------------------------------|------------------------------------|----------------------------------------|
| GPU VRAM (GB)                | `server_config["preload_in_VRAM"]` | MB kept in VRAM for the transformer    |
| SYSTEM RAM (GB)              | `args.perc_reserved_mem_max`       | fraction (0..0.5) of RAM as reserved   |
| Do not use Reserved Memory ✔ | `args.perc_reserved_mem_max = 0.0` | profile 3+ behaviour                    |

Both are **re-read on every model load**, so a change takes effect on the
**next generation** (or after an explicit model unload/reload) — not on the
already-loaded model.

### Notes
- `preload_in_VRAM` primarily biases the **transformer** VRAM budget in profiles
  2 / 4 / 5. If you run a profile that keeps the whole model in VRAM (1 / 3 / 3+),
  the preload lever has little to do because the model is already fully resident.
- **Reserved-memory limitation (be aware):** on Memory Profiles **3 and 4**,
  WanGP's `init_pipe()` forces `pinnedMemory = ["transformer", "transformer2"]`,
  so the transformer stays pinned to reserved RAM *regardless* of
  `perc_reserved_mem_max`. On those profiles the **"Do not use Reserved Memory"**
  checkbox therefore has little effect — the plugin detects this and says so in
  its status line. The **VRAM** control is unaffected and always applies.
  In short: this plugin's main, fully-reliable job is **VRAM control**; the RAM
  side is a best-effort hint that only bites when RAM is the limiting factor.
- The RAM value is turned into a *fraction of your total physical RAM*
  (detected via psutil). Requesting more than 50% is clamped to 0.5 for
  stability, as recommended by WanGP.
- All the "write real variables" logic is isolated in
  `VramRamAdjusterPlugin._apply_to_wgp()` and `_compute_values()`, with
  `# >>> ADJUST HERE <<<` markers if a future WanGP version renames a lever.
