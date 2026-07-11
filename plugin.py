"""
VRAM / RAM Adjuster - WanGP Plugin
==================================

Adjust the Gpu Vram and System Ram - Override the Memory Profiles.

This plugin adds a top-level tab ("Vram/Ram Adj.") that lets the user pick,
with two sliders, exactly how much VRAM (GB) and System RAM (GB) WanGP is
allowed to use, instead of being limited to the predefined Memory Profiles
(1, 2, 3, 3+, 4, 4+, 5).

When the plugin is "activated" (checkbox) it behaves like the built-in
"Override Memory Profile" option (Advanced mode -> Misc): it overrides the
"Default Memory Profile" (video / image / audio) set in
Configuration -> Performance, and pushes the manual VRAM/RAM values into the
mmgp offloader at model-load time.

------------------------------------------------------------------------------
HOW THE MEMORY OVERRIDE WORKS (read this if generation memory is not affected)
------------------------------------------------------------------------------
WanGP loads a model with, roughly:

    generate_video(task, send_cmd, plugin_data=plugin_data, **params)
        -> load_models(model_type, override_profile, **model_kwargs)
            -> offload.profile(pipe,
                               profile_no          = mmgp_profile,
                               perc_reserved_mem_max = perc_reserved_mem_max,
                               budgets             = <optional VRAM budget>,
                               ...)

mmgp (Memory Management for the GPU Poor) exposes the two levers we need:

  * budgets              -> approximate VRAM budget in MEGABYTES allocated to a
                            model in VRAM (the rest is offloaded to RAM). This
                            is how the profiles decide "how much stays in VRAM".
  * perc_reserved_mem_max-> fraction (0..1) of System RAM that may be used as
                            "reserved / pinned / shared memory" to speed up
                            RAM<->VRAM transfers.
                            0.0  == do NOT use reserved memory (this is the
                                    behaviour of profile "3+").

So this plugin translates the two sliders into:

  * GPU VRAM (GB)  -> budget in MB  = round(vram_gb * 1024)
  * SYSTEM RAM (GB)-> perc_reserved_mem_max, computed from the total RAM of the
                      machine, or forced to 0.0 when "Do not use Reserved
                      Memory" is checked (profile 3+ behaviour).

Because the exact plumbing WanGP uses to receive an override from a plugin can
change between versions, all of the "push values into WanGP" logic lives in a
single method: `_apply_to_wgp()`. It uses several strategies (write into
server_config, set globals). Everything you might need to tweak for your exact
WanGP build is tagged with:  # >>> ADJUST HERE <<<
------------------------------------------------------------------------------
"""

import os
import json
import gradio as gr

from shared.utils.plugins import WAN2GPPlugin


# --- Static plugin identity ---------------------------------------------------
PLUGIN_TITLE = "VRAM / RAM Adjuster"
PLUGIN_SUBTITLE = "Adjust the Gpu Vram and System Ram - Override the Memory Profiles"

# Folder (at the root of the plugin folder) where save files are stored.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SAVES_DIR = os.path.join(PLUGIN_DIR, "Saves")

# Slider ranges / defaults (as specified) -------------------------------------
VRAM_MIN, VRAM_MAX, VRAM_STEP, VRAM_DEFAULT = 1.0, 32.0, 0.5, 8.0
RAM_MIN, RAM_MAX, RAM_STEP, RAM_DEFAULT = 16, 128, 2, 32


def _ensure_saves_dir():
    try:
        os.makedirs(SAVES_DIR, exist_ok=True)
    except Exception as e:
        print(f"[VRAM/RAM Adjuster] Could not create Saves dir: {e}")


def _list_saves():
    """Return the list of available save names (without .txt extension)."""
    _ensure_saves_dir()
    try:
        names = []
        for fn in sorted(os.listdir(SAVES_DIR)):
            if fn.lower().endswith(".txt"):
                names.append(fn[:-4])
        return names
    except Exception as e:
        print(f"[VRAM/RAM Adjuster] Could not list saves: {e}")
        return []


def _safe_name(name):
    """Sanitise a user provided save name into a safe filename stem."""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    # remove path separators / quotes / control chars
    for bad in ('/', '\\', ':', '*', '?', '"', "'", '<', '>', '|', '\n', '\r', '\t'):
        name = name.replace(bad, "_")
    return name.strip()


class VramRamAdjusterPlugin(WAN2GPPlugin):

    def __init__(self):
        super().__init__()
        self.name = PLUGIN_TITLE
        self.version = "1.0.0"
        self.description = PLUGIN_SUBTITLE

        # Live state (kept in the plugin instance; reset every app launch).
        # Activation and "Do not use Reserved Memory" are intentionally NOT
        # persisted between launches (spec: both default OFF at every restart).
        self._active = False
        self._vram_gb = VRAM_DEFAULT
        self._ram_gb = RAM_DEFAULT
        self._no_reserved = False
        self._state_component = None   # captured in post_ui_setup

    # -------------------------------------------------------------------------
    # UI SETUP
    # -------------------------------------------------------------------------
    def setup_ui(self):
        # We want to read/modify WanGP's global configuration so the override
        # can act like the built-in "Override Memory Profile".
        # These are injected as attributes on `self` after setup_ui().
        self.request_global("server_config")            # main config dict (holds preload_in_VRAM)
        self.request_global("args")                      # global args obj (holds perc_reserved_mem_max)
        self.request_global("server_config_filename")   # to persist if needed
        # For the "Force Unload Models From RAM" button / auto-unload option:
        self.request_global("release_model")             # frees model + VRAM, sets reload_needed
        self.request_global("any_GPU_process_running")   # guard: don't unload mid-generation
        self.request_component("state")                  # captured in post_ui_setup (guard arg)

        self.add_tab(
            tab_id="vram_ram_adj_tab",
            label="Vram/Ram Adj.",
            component_constructor=self.create_ui,
        )

    def create_ui(self):
        _ensure_saves_dir()

        # ---- Title block ----------------------------------------------------
        gr.Markdown(
            f"<div style='text-align:center'>"
            f"<h1 style='margin-bottom:4px'>{PLUGIN_TITLE}</h1>"
            f"<p style='font-size:15px;color:#9aa0a6;margin-top:0'>{PLUGIN_SUBTITLE}</p>"
            f"</div>"
        )

        gr.Markdown("---")

        # ---- Activation checkbox -------------------------------------------
        activate_cb = gr.Checkbox(
            label="Activate the plugin",
            value=False,   # OFF by default, every launch
            info="When ticked, overrides the Default Memory Profiles "
                 "(video / image / audio) using the manual values below.",
        )

        # ---- VRAM slider ----------------------------------------------------
        gr.Markdown("### GPU VRAM - Gb")
        vram_slider = gr.Slider(
            minimum=VRAM_MIN,
            maximum=VRAM_MAX,
            step=VRAM_STEP,
            value=VRAM_DEFAULT,
            label="GPU VRAM - Gb",
            show_label=False,
        )

        # ---- Explanatory note under the vRAM slider -------------------------
        gr.Markdown(
            "<div style='font-size:13px;color:#9aa0a6;margin-top:2px'>"
            "ℹ️ <b>Note about the VRAM setting:</b> Allow for a margin of at least 2 GB below "
            "your GPU's VRAM capacity —or even more if you are using LoRAs."
            "Monitor VRAM usage and adjust as necessary; avoid maxing out your VRAM"
            "</div>"

        )

        # ---- RAM slider -----------------------------------------------------
        gr.Markdown("### SYSTEM RAM - Gb")
        ram_slider = gr.Slider(
            minimum=RAM_MIN,
            maximum=RAM_MAX,
            step=RAM_STEP,
            value=RAM_DEFAULT,
            label="SYSTEM RAM - Gb",
            show_label=False,
        )

        # ---- Explanatory note under the RAM slider -------------------------
        gr.Markdown(
            "<div style='font-size:13px;color:#9aa0a6;margin-top:2px'>"
            "ℹ️ <b>Note about the RAM setting:</b> whether it takes effect "
            "depends on the selected <b>Memory Profile</b>. On profiles "
            "<b>3</b> and <b>4</b>, WanGP pins the main model to reserved RAM "
            "by design, so the RAM value (and \"Do not use Reserved Memory\") "
            "has little effect there. The <b>VRAM setting above always works</b>, "
            "whatever the profile."
            "</div>"
        )

        # ---- Reserved memory checkbox --------------------------------------
        no_reserved_cb = gr.Checkbox(
            label="Do not use Reserved Memory",
            value=False,   # OFF by default, every launch
            info="Unticked: RAM used like Memory Profiles 1/2/3/4/4+/5. "
                 "Ticked: RAM used like Memory Profile 3+ (no reserved / "
                 "shared memory, only the RAM itself). "
                 "Effectiveness depends on the active profile (see note above).",
        )

        # ---- Auto-unload option --------------------------------------------
        auto_unload_cb = gr.Checkbox(
            label="Auto-unload models when a parameter changes",
            value=False,   # OFF by default, every launch
            info="When ticked, models are automatically unloaded from RAM/VRAM "
                 "as soon as you change a value here (or load a preset), so the "
                 "next generation reloads them with the new settings. No need to "
                 "press the button below.",
        )

        # Status line (feedback to the user)
        status = gr.Markdown("", elem_id="vram_ram_adj_status")

        gr.Markdown("---")

        # ---- Save / Load section -------------------------------------------
        gr.Markdown("## Save / Load")

        with gr.Row():
            saves_dd = gr.Dropdown(
                choices=_list_saves(),
                value=None,
                label="Saved parameters",
                interactive=True,
                scale=3,
            )
            save_btn = gr.Button("Save", scale=1)
            load_btn = gr.Button("Load", scale=1)
            delete_btn = gr.Button("Delete", scale=1)

        # ---- Save name modal (hidden until "Save" is pressed) --------------
        with gr.Group(visible=False) as save_modal:
            gr.Markdown("**Enter a name for this save:**")
            with gr.Row():
                save_name_tb = gr.Textbox(
                    label="Save name",
                    show_label=False,
                    placeholder="my_settings",
                    scale=3,
                )
                save_ok_btn = gr.Button("OK", scale=1)
                save_cancel_btn = gr.Button("CANCEL", scale=1)

        # ---- Delete confirmation modal (hidden until "Delete" is pressed) --
        with gr.Group(visible=False) as delete_modal:
            delete_prompt = gr.Markdown("**Delete the selected save?**")
            with gr.Row():
                delete_yes_btn = gr.Button("Yes", scale=1)
                delete_no_btn = gr.Button("No", scale=1)

        # =====================================================================
        # EVENT WIRING
        # =====================================================================

        # --- live state + apply override on every control change ------------
        def _on_change(active, vram, ram, no_reserved, auto_unload):
            self._active = bool(active)
            self._vram_gb = float(vram)
            self._ram_gb = float(ram)
            self._no_reserved = bool(no_reserved)
            msg = self._apply_to_wgp()
            # Auto-unload if requested and the plugin is active.
            if auto_unload and self._active:
                unload_msg = self._do_unload()
                msg = f"{msg}\n\n{unload_msg}"
            return msg

        _change_inputs = [activate_cb, vram_slider, ram_slider, no_reserved_cb,
                          auto_unload_cb]

        activate_cb.change(_on_change, inputs=_change_inputs, outputs=[status])
        vram_slider.change(_on_change, inputs=_change_inputs, outputs=[status])
        ram_slider.change(_on_change, inputs=_change_inputs, outputs=[status])
        no_reserved_cb.change(_on_change, inputs=_change_inputs, outputs=[status])

        # --- SAVE: open the naming modal ------------------------------------
        def _open_save_modal():
            return gr.update(visible=True), ""

        save_btn.click(
            _open_save_modal,
            inputs=[],
            outputs=[save_modal, save_name_tb],
        )

        # --- SAVE OK: write the .txt file, refresh dropdown, close modal ----
        def _do_save(name, vram, ram, no_reserved):
            stem = _safe_name(name)
            if not stem:
                return (
                    gr.update(visible=True),          # keep modal open
                    gr.update(),                       # dropdown unchanged
                    "⚠️ Please enter a valid name.",
                )
            ok, msg = self._write_save(stem, float(vram), float(ram), bool(no_reserved))
            choices = _list_saves()
            new_value = stem if ok and stem in choices else None
            return (
                gr.update(visible=False),
                gr.update(choices=choices, value=new_value),
                msg,
            )

        save_ok_btn.click(
            _do_save,
            inputs=[save_name_tb, vram_slider, ram_slider, no_reserved_cb],
            outputs=[save_modal, saves_dd, status],
        )

        # --- SAVE CANCEL: just close the modal ------------------------------
        def _cancel_save():
            return gr.update(visible=False)

        save_cancel_btn.click(_cancel_save, inputs=[], outputs=[save_modal])

        # --- LOAD: only if a save is selected -------------------------------
        def _do_load(selected, active, auto_unload):
            if not selected:
                return (
                    gr.update(), gr.update(), gr.update(),
                    "⚠️ No save selected.",
                )
            data = self._read_save(selected)
            if data is None:
                return (
                    gr.update(), gr.update(), gr.update(),
                    f"⚠️ Could not read save '{selected}'.",
                )
            vram, ram, no_reserved = data
            # push into live state and re-apply if active
            self._vram_gb = vram
            self._ram_gb = ram
            self._no_reserved = no_reserved
            self._active = bool(active)
            apply_msg = self._apply_to_wgp()
            msg = f"✅ Loaded '{selected}'. " + apply_msg
            # Auto-unload if requested and the plugin is active.
            if auto_unload and self._active:
                msg = f"{msg}\n\n{self._do_unload()}"
            return (
                gr.update(value=vram),
                gr.update(value=ram),
                gr.update(value=no_reserved),
                msg,
            )

        load_btn.click(
            _do_load,
            inputs=[saves_dd, activate_cb, auto_unload_cb],
            outputs=[vram_slider, ram_slider, no_reserved_cb, status],
        )

        # --- DELETE: open confirmation modal (only if a save is selected) ---
        def _open_delete_modal(selected):
            if not selected:
                return gr.update(visible=False), "⚠️ No save selected."
            return (
                gr.update(visible=True),
                gr.update(),
            )

        delete_btn.click(
            _open_delete_modal,
            inputs=[saves_dd],
            outputs=[delete_modal, status],
        )

        # keep the confirmation prompt naming the selected file
        def _sync_delete_prompt(selected):
            if selected:
                return gr.update(value=f"**Delete the save '{selected}' ?**")
            return gr.update()

        saves_dd.change(_sync_delete_prompt, inputs=[saves_dd], outputs=[delete_prompt])

        # --- DELETE YES: remove file, refresh, close ------------------------
        def _do_delete(selected):
            if not selected:
                return gr.update(visible=False), gr.update(), "⚠️ No save selected."
            ok, msg = self._delete_save(selected)
            choices = _list_saves()
            return (
                gr.update(visible=False),
                gr.update(choices=choices, value=None),
                msg,
            )

        delete_yes_btn.click(
            _do_delete,
            inputs=[saves_dd],
            outputs=[delete_modal, saves_dd, status],
        )

        # --- DELETE NO: just close the modal --------------------------------
        def _cancel_delete():
            return gr.update(visible=False)

        delete_no_btn.click(_cancel_delete, inputs=[], outputs=[delete_modal])

        # ---- Force Unload section (bottom of the plugin) -------------------
        gr.Markdown("---")
        force_unload_btn = gr.Button("Force Unload Models From RAM", variant="secondary")
        gr.Markdown(
            "<div style='font-size:13px;color:#9aa0a6;margin-top:2px'>"
            "After changing any setting, it is best to unload the models so they "
            "get <b>reloaded with the new parameters</b> on the next generation. "
            "Use this button (same action as Configuration → Performance), or "
            "tick <b>\"Auto-unload models when a parameter changes\"</b> above to "
            "do it automatically."
            "</div>"
        )

        def _force_unload():
            return self._do_unload()

        force_unload_btn.click(_force_unload, inputs=[], outputs=[status])

    # -------------------------------------------------------------------------
    # LIFECYCLE: capture injected components
    # -------------------------------------------------------------------------
    def post_ui_setup(self, components):
        """Capture the main gr.State component (used as guard arg on unload)."""
        try:
            if isinstance(components, dict):
                self._state_component = components.get("state", None)
        except Exception:
            self._state_component = None
        return {}

    # -------------------------------------------------------------------------
    # MODEL UNLOAD
    # -------------------------------------------------------------------------
    def _do_unload(self):
        """
        Unload the loaded models from RAM/VRAM so they are reloaded with the
        current settings on the next generation.

        Core action is wgp.release_model() (module-level, no args). We guard
        against unloading while a generation/GPU process is running, when we
        can obtain that information; otherwise we proceed (release_model itself
        is safe to call and simply flags reload_needed).
        """
        release_model = getattr(self, "release_model", None)
        if release_model is None:
            return "⚠️ Unload unavailable: 'release_model' not accessible."

        # Best-effort guard: only block if we can positively confirm a GPU
        # process is running. We use ignore_main=True (same spirit as the
        # native pause handler) so normal idle UI interaction is never blocked.
        guard = getattr(self, "any_GPU_process_running", None)
        state_value = getattr(self, "_state_component", None)
        try:
            if guard is not None and state_value is not None:
                if guard(state_value, "unload", ignore_main=True):
                    return ("⏳ Not unloaded: a plugin/GPU process is currently "
                            "running. Try again once it has finished.")
        except Exception:
            # If the guard can't be evaluated, fall through and unload anyway.
            pass

        try:
            release_model()
            return ("♻️ Models unloaded from RAM/VRAM. They will reload with "
                    "the current settings on the next generation.")
        except Exception as e:
            return f"⚠️ Unload failed: {e}"

    # -------------------------------------------------------------------------
    # SAVE / LOAD FILE HELPERS
    # -------------------------------------------------------------------------
    def _save_path(self, stem):
        return os.path.join(SAVES_DIR, f"{stem}.txt")

    def _write_save(self, stem, vram_gb, ram_gb, no_reserved):
        _ensure_saves_dir()
        payload = {
            "vram_gb": vram_gb,
            "ram_gb": ram_gb,
            "no_reserved_memory": bool(no_reserved),
        }
        try:
            with open(self._save_path(stem), "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            return True, f"✅ Saved '{stem}'."
        except Exception as e:
            return False, f"⚠️ Save failed: {e}"

    def _read_save(self, stem):
        path = self._save_path(stem)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            vram = float(data.get("vram_gb", VRAM_DEFAULT))
            ram = float(data.get("ram_gb", RAM_DEFAULT))
            no_reserved = bool(data.get("no_reserved_memory", False))
            # clamp to valid ranges just in case the file was edited by hand
            vram = min(max(vram, VRAM_MIN), VRAM_MAX)
            ram = min(max(ram, RAM_MIN), RAM_MAX)
            return vram, ram, no_reserved
        except Exception as e:
            print(f"[VRAM/RAM Adjuster] Could not read save '{stem}': {e}")
            return None

    def _delete_save(self, stem):
        path = self._save_path(stem)
        if not os.path.isfile(path):
            return False, f"⚠️ Save '{stem}' not found."
        try:
            os.remove(path)
            return True, f"🗑️ Deleted '{stem}'."
        except Exception as e:
            return False, f"⚠️ Delete failed: {e}"

    # -------------------------------------------------------------------------
    # MEMORY OVERRIDE LAYER
    # -------------------------------------------------------------------------
    #
    # How WanGP actually consumes memory settings (verified against wgp.py):
    #
    #   load_models(model_type, override_profile, output_type, ...)
    #       profile = compute_profile(override_profile, output_type)
    #       mmgp_profile = init_pipe(pipe, kwargs, profile)
    #           # init_pipe reads:  preload = int(args.preload)
    #           #                   if preload == 0:
    #           #                       preload = server_config.get("preload_in_VRAM", 0)
    #           #   -> preload (in MB) becomes the VRAM budget of 'transformer'
    #           #      for profiles 2 / 4 / 5.
    #       perc_reserved_mem_max = args.perc_reserved_mem_max
    #       offload.profile(pipe, profile_no=mmgp_profile,
    #                       perc_reserved_mem_max=perc_reserved_mem_max, ...)
    #
    # So the two real levers a plugin must set are:
    #   * VRAM budget  ->  server_config["preload_in_VRAM"]   (MEGABYTES)
    #   * Reserved RAM ->  args.perc_reserved_mem_max          (fraction 0..0.5)
    #
    # Both are re-read every time a model is loaded, so changing them takes
    # effect on the *next* model load (i.e. the next generation, or after an
    # explicit model unload/reload).
    # -------------------------------------------------------------------------

    def _compute_values(self):
        """Translate the two sliders + checkbox into the real wgp.py values."""
        # VRAM: GB -> MB, used as server_config["preload_in_VRAM"].
        vram_preload_mb = int(round(self._vram_gb * 1024))

        # RAM -> perc_reserved_mem_max (fraction of physical RAM used as
        # reserved / pinned / shared memory).
        #   * "Do not use Reserved Memory" ticked -> 0.0  (profile 3+ behaviour)
        #   * otherwise -> requested RAM as a fraction of total physical RAM,
        #     clamped to [0.0, 0.5] as recommended by WanGP for stability.
        if self._no_reserved:
            perc_reserved = 0.0
        else:
            total_ram_gb = self._detect_total_ram_gb()
            if total_ram_gb and total_ram_gb > 0:
                frac = self._ram_gb / float(total_ram_gb)
            else:
                frac = 0.40  # sane fallback (WanGP default)
            perc_reserved = float(min(max(frac, 0.0), 0.5))

        return vram_preload_mb, perc_reserved

    def _detect_total_ram_gb(self):
        """Best-effort detection of total system RAM in GB (no hard dep)."""
        try:
            import psutil
            return psutil.virtual_memory().total / (1024 ** 3)
        except Exception:
            pass
        try:
            return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
        except Exception:
            return None

    def _apply_to_wgp(self):
        """
        Write (or restore) the real WanGP memory variables so the next model
        load honours the manual VRAM/RAM values.

        VRAM  -> server_config["preload_in_VRAM"]  (MB)
        RAM   -> args.perc_reserved_mem_max        (fraction)

        On deactivation the original values are restored so WanGP falls back to
        its normal behaviour.
        """
        server_config = getattr(self, "server_config", None)
        args = getattr(self, "args", None)

        # --- remember the original values once, so we can restore them --------
        if not hasattr(self, "_orig_captured"):
            self._orig_captured = True
            self._orig_preload = (
                server_config.get("preload_in_VRAM", 0)
                if isinstance(server_config, dict) else 0
            )
            self._orig_perc = (
                getattr(args, "perc_reserved_mem_max", None)
                if args is not None else None
            )

        # --- deactivated: restore originals -----------------------------------
        if not self._active:
            if isinstance(server_config, dict):
                server_config["preload_in_VRAM"] = self._orig_preload
            if args is not None and self._orig_perc is not None:
                try:
                    args.perc_reserved_mem_max = self._orig_perc
                except Exception:
                    pass
            return ("Plugin inactive — original memory settings restored "
                    "(takes effect on next model load).")

        # --- active: push the manual values -----------------------------------
        vram_preload_mb, perc_reserved = self._compute_values()

        ok_vram = ok_ram = False

        if isinstance(server_config, dict):
            # >>> ADJUST HERE <<<  VRAM budget lever for your WanGP build.
            server_config["preload_in_VRAM"] = vram_preload_mb
            ok_vram = True
        else:
            print("[VRAM/RAM Adjuster] WARNING: server_config unavailable; "
                  "cannot set preload_in_VRAM.")

        if args is not None:
            try:
                # >>> ADJUST HERE <<<  Reserved-RAM lever for your WanGP build.
                args.perc_reserved_mem_max = perc_reserved
                ok_ram = True
            except Exception as e:
                print(f"[VRAM/RAM Adjuster] WARNING: could not set "
                      f"args.perc_reserved_mem_max: {e}")
        else:
            print("[VRAM/RAM Adjuster] WARNING: 'args' global unavailable; "
                  "cannot set perc_reserved_mem_max.")

        ram_desc = ("no reserved memory (profile 3+)"
                    if self._no_reserved
                    else f"{perc_reserved:.2f} of RAM (~{self._ram_gb:g} GB target)")

        # --- honest note about the reserved-memory limitation -----------------
        # On profiles 3 and 4, WanGP's init_pipe() forces
        #   kwargs["pinnedMemory"] = ["transformer", "transformer2"]
        # so the transformer stays pinned to reserved RAM regardless of
        # perc_reserved_mem_max. In that case "Do not use Reserved Memory"
        # cannot fully take effect. We detect the active profile and tell the
        # user plainly instead of pretending it worked.
        reserved_note = ""
        if self._no_reserved:
            active_profile = self._detect_active_profile(server_config)
            if active_profile in (3, 4, 3.5, 4.5):
                reserved_note = (
                    f" ⚠️ Note: on Memory Profile {active_profile:g}, WanGP "
                    f"pins the transformer to reserved RAM by design, so "
                    f"'Do not use Reserved Memory' has little effect here. "
                    f"The VRAM setting still applies fully."
                )

        prefix = "✅ Override active" if (ok_vram and ok_ram) else "⚠️ Partial override"
        return (
            f"{prefix} — VRAM preload: {vram_preload_mb} MB "
            f"({self._vram_gb:g} GB) · reserved RAM: {ram_desc}. "
            f"Applies on the next model load (start a generation, or unload/"
            f"reload the model)."
            + reserved_note
        )

    def _detect_active_profile(self, server_config):
        """
        Best-effort read of the currently active VIDEO memory profile.
        Returns a number (1,2,3,3.5,4,4.5,5) or None if unknown.
        """
        # Prefer the profile of the last loaded model if wgp exposes it.
        try:
            import importlib
            wgp = importlib.import_module("wgp")
            lp = getattr(wgp, "loaded_profile", None)
            if isinstance(lp, (int, float)) and lp >= 0:
                return lp
            fp = getattr(wgp, "force_profile_no", None)
            if isinstance(fp, (int, float)) and fp >= 0:
                return fp
        except Exception:
            pass
        # Fall back to the configured video profile.
        if isinstance(server_config, dict):
            vp = server_config.get("video_profile", None)
            try:
                if vp is not None:
                    return float(vp) if str(vp).replace(".", "", 1).isdigit() else vp
            except Exception:
                return vp
        return None
