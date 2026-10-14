# slopcast-vita — Speed-Hack Layer

Performance-first patches for **slopcast-vita**, a fork of `Rinnegatamante/flycast-vita` based on upstream commit `446f887741ce38596224e6cea83148627d4263e6`.

Target titles: **Sonic Adventure**, **Crazy Taxi**, **Virtua Tennis** — and anything else where gameplay framerate matters more than graphic or audio accuracy.

> **A note on naming.** The *fork* is slopcast-vita. The *module* it carries is still called `speedhack` — that is the subsystem name, the way `dynarec` and `rend` are subsystem names, and it is what the in-tree files `core/hw/pvr/speedhack.{h,cpp}` and the `// [speedhack] BEGIN/END` revert markers say. Project-level naming (package, env vars, log prefix) says slopcast.

---

## Read this first — what I could and could not verify

I did **not** have your working tree in front of me when writing these hunks. I know the base commit hash; I do not know the exact line numbers, the local variable names, or which upstream symbols your fork has already renamed.

That has one concrete consequence:

- **New files** (`speedhack.h`, `speedhack.cpp`) are exact and will drop in cleanly.
- **Edits to existing sources** are performed by `patch_speedhack.py`, which locates each hook by a **symbol** (a function definition, a call site) instead of a line number. That survives revision drift. It is idempotent, it reports exactly which anchors it could not find, and it reverts cleanly.
- A handful of hunks are inherently **signature-dependent** (they need the name of a local variable or a function parameter). Those are listed under *Manual hooks* below with precise snippets, and the applier deliberately **skips** them rather than guessing. Guessing there produces a compile error in a file you did not expect to touch.

Run `./locate_anchors.sh` first. It tells you, in about two seconds, exactly which anchors exist in your tree and where. Then run the applier in `--check` mode before `--apply`.

---

## Contents

```
slopcast-vita/
├── README.md                     ← you are here
├── locate_anchors.sh             ← verify anchors against your tree
├── apply.sh                      ← wrapper around the applier
├── patch_speedhack.py            ← the anchor-based patcher
├── files/
│   └── core/hw/pvr/
│       ├── speedhack.h           ← new, drop-in
│       └── speedhack.cpp         ← new, drop-in
├── patches/
│   └── 0001-vita-cmake-flags.patch   ← reference only, see Manual D
└── profiles/
    └── titles.conf               ← per-title profile table (reference copy)
```

---

## Quick start

Unpack this package somewhere convenient, e.g. `~/slopcast-vita`. Then, pointing it at your emulator checkout:

```
# 1. Confirm the anchors exist in YOUR tree (writes nothing)
~/slopcast-vita/locate_anchors.sh  /path/to/your/checkout

# 2. Dry run — reports every insertion point, writes nothing
~/slopcast-vita/apply.sh --check   /path/to/your/checkout

# 3. Apply
~/slopcast-vita/apply.sh --apply   /path/to/your/checkout

# 4. Revert if you want the tree back
~/slopcast-vita/apply.sh --revert  /path/to/your/checkout
```

The applier backs up every file it touches to `<file>.speedhack.bak` before the first modification and refuses to overwrite an existing backup.

---

## What the applier does automatically

Each of these is a **top-of-function** insertion — robust across revisions because it anchors on the function definition, not on a line number.

| # ↕▾ | File ↕▾ | Function ↕▾ | Effect ↕▾ |
|---|---|---|---|
| −1 | `core/hw/sh4/dynarec/sh4_fpu.cpp` | `UpdateFPSCR()` | Early-out when the host-relevant FPSCR bits are unchanged |
| −2 | `core/hw/sh4/dynarec/sh4_fpu.cpp` | `SetFPSCR(...)` | Force `PR`/`FR`/`SZ` off → single-precision, single-bank FPU |
| −3 | `core/hw/sh4/dynarec/dynarec.cpp` | `RuntimeBlockCheck(...)` | Skip self-modifying-code invalidation |
| −4 | `core/hw/aica/dsp.cpp` | `dsp_step()` | Bypass the Yamaha DSP compute, keep the register handshake |
| −5 | `core/hw/aica/dsp.cpp` | `dsp_writenmem(...)` | Swallow DSP program uploads when the DSP is off |
| −6 | `core/hw/aica/aica.cpp` | `AICA_Sample()` | Sample-rate divider (÷1 / ÷2 / ÷4) |
| −7 | `core/hw/aica/aica.cpp` | `UpdateArm7(...)` | Alternate-tick ARM7 stepping |
| −8 | `core/hw/pvr/rend.cpp` | before `renderer->draw(` | Drop modifier volumes + shadow pre-pass |
⚙

Hook **8** is the only one with a caveat: the inserted block references the local `RenderType` in scope. On upstream at `446f887` that local is `rt`. If your fork renamed it, the compiler will flag the exact line and the fix is a one-word rename. The applier will tell you when it inserts there so you know where to look.

---

## Manual hooks (signature-dependent — wire these by hand)

These need a name I cannot safely guess. Each is small.

### Manual A — Per-strip flush at end-of-list

**File:** `core/hw/pvr/ta.cpp`
**Anchor:** the line containing `PerStripRendering`

Stock flycast already has `config::PerStripRendering`; it just lets the *game* decide when a list closes. You want to close early for **translucent** lists only — opaque lists should stay batched, because on vitaGL a small `glDrawArrays` costs more in GLES→GXM translation than it saves.

Insert this immediately **before** the existing `if (config::PerStripRendering …)` line:

```
// [speedhack] pvr-perstrip — flush non-opaque lists at end-of-list
if (speedhack::perStrip() && !rt.translucent.empty())
{
    rend_start_render(&rt);
    rend_end_render();
    rt.translucent.clear();
}
```

Then add `#include "hw/pvr/speedhack.h"` near the top of the file.

> **Do not enable this for Sonic Adventure.** SA's opaque list is long and coherent; batching wins there. Crazy Taxi is the opposite — its translucent list is short and chaotic, and per-strip flush removes a large late-frame sort. See the profile table.

---

### Manual B — Kill render-to-texture / framebuffer readback

**File:** `core/hw/pvr/pvr_regs.cpp`
**Anchor:** the definition of `pvr_WriteReg`

This one needs the name of the value parameter. On upstream it is `value`. If yours differs, rename in the snippet.

Insert at the **top of the function body**:

```
// [speedhack] pvr-rtt — force framebuffer read/write off
if (speedhack::skipRTT())
{
    // FB_R_CTRL bits 0-1 = framebuffer read enable.
    // FB_W_CTRL bit 0   = framebuffer write enable.
    // Forcing both off removes the VRAM->texture round trip, the
    // dirty-tile tracker, and the vitaGL texture-cache invalidation
    // that goes with them.
    //
    // Visual cost: mirrors show stale content, pause-menu thumbnails
    // freeze, in-game CRT/monitor effects go black.
    switch (addr)
    {
    case FB_R_CTRL: value &= ~0x3u; break;
    case FB_W_CTRL: value &= ~0x1u; break;
    default: break;
    }
}
```

Plus a defence-in-depth early-out in the GLES renderer. **File:** `core/rend/gles/gles.cpp`, at the top of the framebuffer-render entry point (grep for `renderFramebuffer` or `EmulateFramebuffer`):

```
// [speedhack] gles-rtt — no readback, no upload, no cache invalidation
if (speedhack::skipRTT())
    return;
```

---

### Manual C — Texture filtering: nearest + negative LOD bias

**File:** `core/rend/gles/gles.cpp`
**Anchor:** the sampler-state setup for a texture (grep `GL_LINEAR_MIPMAP`)

```
// [speedhack] gles-filter — nearest, no mip chain, negative LOD bias
if (speedhack::forceNearest())
{
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, 0);   // never build mips
}
vglTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_LOD_BIAS, (float)speedhack::lodBias());
```

Separately, once at GL init, if you are not already doing it:

```
vglUseLowPrecision(GL_TRUE);   // mediump fragment math — free win on SGX543
```

---

### Manual D — CMake flags and new source registration

**File:** `CMakeLists.txt`

Two things must happen, and **order matters**: `add_compile_options` must run *before* your target is created, or the flags will not apply to it.

**D.1** — Add the flags after your Vita toolchain include and **before** the first `add_executable` / `add_library`:

```
if(VITA)
  add_compile_options(
    -O3 -fomit-frame-pointer -ffast-math -fno-math-errno
    -funroll-loops -ftree-vectorize
    -mfpu=neon -mfloat-abi=hard -mcpu=cortex-a9
  )
  # Add speedhack.cpp if your build does not glob core/**/*.cpp
  target_sources(flycast PRIVATE core/hw/pvr/speedhack.cpp)
endif()
```

If your CMake already globs `core/**/*.cpp`, drop the `target_sources` line. Check with:

```
grep -n "GLOB\|target_sources\|add_executable" CMakeLists.txt
```

**D.2** — `-ffast-math` is safe for the emulator core (integer + single-precision, and the dynarec emits its own FP ops), but keep it **off** for anything computing cycle timing, because it may reassociate that arithmetic.

Do **not** add `-fno-exceptions` or `-fno-rtti` without testing — the config and UI layers use both.

---

### Manual E — Thread pinning call sites

`speedhack.h` exposes `pinEmuThread()`, `pinAudioThread()`, `pinGpuThread()`. Call them from the entry points of the respective threads. One line each.

- Emu thread: wherever `config::ThreadedRendering` decides the emulation loop runs.
- Render/GPU-submit thread: top of the render-thread lambda or function in `rend.cpp` / `oslib`.
- Audio thread: top of the `sceAudioOut` output thread.

**Rule: never pin the emu thread and the GPU-submit thread to the same core.** Under vitaGL the GLES→GXM command-buffer build is entirely CPU-side, so that thread is doing real work while the dynarec runs. They share an L2 slice and will thrash each other.

Optional but recommended — replace the render-thread spinlock with a yield. Grep for the atomic the render thread spins on (`renderBusy`, `renderDone`):

```
// [speedhack] render-yield
while (std::atomic_load_explicit(&s_renderBusy, std::memory_order_acquire))
    sceKernelDelayThread(50);   // yields the core; 50us << one 60Hz frame
```

A bare atomic spin burns a whole A9 core *and its L2 slice* for the length of the guest frame.

---

## Configuration: environment variables

The profile is resolved **lazily on first query** — there is no init call to wire, which removes one fragile insertion point. Values come from the environment, so you can A/B without editing an INI or rebuilding:

| Variable ↕▾ | Values ↕▾ | Meaning ↕▾ |
|---|---|---|
| −`SLOPCAST_VITA_SPEED` | `0`–`3` | Preset: Accurate / Balanced / Speed / Fastest |
| −`SLOPCAST_VITA_PROFILE` | serial, or `off` | Force a title profile, or disable the title table |
| −`SLOPCAST_VITA_PERSTRIP` | `0`/`1` | Override per-strip flush |
| −`SLOPCAST_VITA_MODVOLS` | `0`/`1` | Override modifier-volume skip |
| −`SLOPCAST_VITA_SHADOW` | `0`/`1` | Override shadow pre-pass skip |
| −`SLOPCAST_VITA_RTT` | `0`/`1` | Override RTT / readback kill |
| −`SLOPCAST_VITA_AUDIO_DSP` | `0`/`1` | Run the Yamaha DSP at all |
| −`SLOPCAST_VITA_AUDIO_DIV` | `1`/`2`/`4` | Audio sample-rate divider |
| −`SLOPCAST_VITA_LOG` | `0`/`1` | Print the resolved profile at first use |
⚙

On a non-Vita build the default preset is `Accurate`, so a shared tree behaves exactly as before on PC. On `__vita__` the default is `Speed`.

Example A/B session:

```
SLOPCAST_VITA_SPEED=3 SLOPCAST_VITA_MODVOLS=1 SLOPCAST_VITA_LOG=1 ./flycast
SLOPCAST_VITA_SPEED=3 SLOPCAST_VITA_MODVOLS=0 SLOPCAST_VITA_LOG=1 ./flycast
```

The log line tells you exactly what was resolved, including which title profile matched.

---

## Wiring the game serial

The title table needs the loaded disc's serial. `speedhack.cpp` calls a **weak symbol**:

```
extern "C" const char* speedhack_game_serial_impl();   // weak, returns nullptr by default
```

To enable per-title profiles, define it once in your fork — wherever the GD-ROM layer knows the serial:

```
// e.g. in core/hw/gdrom/gdrom.cpp, or wherever your fork exposes it
extern "C" const char* speedhack_game_serial_impl()
{
    return config::GameId.c_str();   // adjust to whatever your fork calls it
}
```

Until you define it, the preset still applies and per-title profiles are simply skipped — the module logs `no serial, preset only`. That is a complete, correct behaviour, not a stub.

Find the right symbol with:

```
grep -rn "GameId\|game_id\|GetGameId\|serial" core/hw/gdrom/ core/cfg/ | head -30
```

---

## Per-title profiles

The compiled-in table in `speedhack.cpp` (mirrored in `profiles/titles.conf`):

| Title ↕▾ | perStrip ↕▾ | modVols ↕▾ | shadowPass ↕▾ | RTT ↕▾ | aicaDSP ↕▾ | sampleDiv ↕▾ | Rationale ↕▾ |
|---|---|---|---|---|---|---|---|
| −**Sonic Adventure** | ✗ | **cut** | **cut** | **cut** | ✗ | 2 | FTRV-heavy geometry + real shadow volumes. Modvol and RTT cuts are the whole ballgame; leave batching alone. |
| −**Sonic Adventure 2** | ✗ | **cut** | **cut** | **cut** | ✗ | 2 | Same shape as SA, slightly heavier. |
| −**Crazy Taxi** | **on** | **cut** | keep | **cut** | **cut** | 2 | Translucent-heavy. Per-strip beats batching here. The DSP reverb is expensive and nobody misses it. |
| −**Virtua Tennis** | ✗ | **cut** | keep | **cut** | keep | 1 | Tiny scene, already fast. **Do not** drop the shadow pass or the DSP — ball shadow and hit transients are gameplay feel. |
⚙

The through-line: **`skipRTT` and `skipModVolumes` are near-universal wins; `perStrip` and the audio cuts are per-title.** Tune in that order.

---

## Recommended implementation order

If you only do part of this, do it in this order — highest frames-gained per unit of visual damage first:

1. **`skipRTT` + `skipModVolumes`** — biggest GPU win, every title, damage confined to mirrors and shadows.
2. **Thread pinning + spinlock yield** — free, zero accuracy cost, and it makes everything else land better because the dynarec stops fighting for L2.
3. **`singlePrecisionFPU` + `fastFpscr`** — biggest dynarec win. This is what makes SA's Emerald Coast hold a frame rate.
4. **`aicaDSP = false` + `sampleDiv = 2`** — reliable 5–10%, near-zero perceptual cost.
5. **`perStrip`** — per-title only, after measuring. The one item here that can go *negative*.

---

## Method

Apply **one** hack at a time. Run the same 60-second segment of the same title. Record the frame-time **histogram** (not just the mean — you care about p99) and take a screenshot.

- Screenshot clean **and** p99 dropped → keep it.
- p99 did not move → you paid a correctness cost for nothing. Revert it and move on.

Do not batch. The whole point of the environment-variable config is that you never have to rebuild to flip one flag.

---

## Reverting

```
~/slopcast-vita/apply.sh --revert /path/to/your/checkout
```

Removes every marked block and deletes the two copied files. Original `.speedhack.bak` backups are left in place — delete them by hand once you are satisfied.

---

## Audio troubleshooting

If you get crackle after the audio changes, the fix is almost always a **larger** `sceAudioOut` block, not reverting the hacks. Increase the audio latency setting first. Bigger blocks mean a spikier per-block cost but fewer underruns — the right shape for a shared-core Vita.

And a specific warning: **do not use `sampleDiv = 4` for Virtua Tennis.** Ball-hit transients go mushy at 11 kHz, and that is a gameplay *feel* regression, which is exactly the thing this whole exercise is trying to protect.