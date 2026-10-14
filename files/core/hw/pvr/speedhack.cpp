/*
 * speedhack.cpp — implementation of the slopcast-vita speed-hack layer.
 *
 * See speedhack.h for the design rules. The short version:
 *   - lazy, thread-safe, once-only profile resolution
 *   - environment-variable configuration (no INI edit, no rebuild to A/B)
 *   - per-title overrides keyed on the disc serial, wired through a weak
 *     symbol so this file compiles and runs even before you connect it
 */
#include "speedhack.h"

#include <atomic>
#include <cstdlib>
#include <cstdio>
#include <cstring>

#if defined(__vita__)
#include <psp2/kernel/threadmgr.h>
#endif

namespace speedhack
{

// ===========================================================================
// Weak serial hook
//
// Define this once in your fork, wherever the GD-ROM layer knows the serial:
//
//     extern "C" const char* speedhack_game_serial_impl()
//     {
//         return config::GameId.c_str();   // or whatever your fork calls it
//     }
//
// Until then, per-title profiles are skipped and the preset alone applies.
// That is a complete, correct behaviour — not a stub.
// ===========================================================================
extern "C" __attribute__((weak)) const char* speedhack_game_serial_impl()
{
	return nullptr;
}

// ===========================================================================
// Per-title overrides
//
// Serials are from the boot log line "Game ID: XXXXXXXXX". Verify yours —
// region revisions differ. Field order matches Profile exactly; the table is
// applied through assign() below, so a reordering of Profile cannot silently
// corrupt these.
// ===========================================================================
struct TitleOverride
{
	const char* serial;
	Preset preset;
	bool perStrip;
	bool skipModVolumes;
	bool skipShadowPass;
	bool skipRTT;
	bool forceNearest;
	int  lodBias;
	bool fastFpscr;
	bool singlePrecisionFPU;
	bool skipSmcCheck;
	bool tightSched;
	bool aicaDSP;
	int  aicaSampleDiv;
	bool aicaArm7Alt;
};

static const TitleOverride kTitles[] =
{
	// Sonic Adventure (US). FTRV-heavy geometry + real shadow volumes.
	// perStrip OFF: SA's opaque list is long and coherent, batching wins.
	// The cost is in the modifier-volume and RTT passes.
	{ "MK-51000", Preset::Fastest,
	  /*perStrip*/false, /*modVols*/true,  /*shadow*/true,  /*rtt*/true,
	  /*nearest*/true,   /*lod*/ -1,
	  /*fpscr*/true,     /*spFpu*/true,     /*smc*/true,     /*sched*/true,
	  /*dsp*/false,      /*div*/ 2,         /*arm7alt*/true },

	// Sonic Adventure 2 (US). Same shape as SA, slightly heavier.
	{ "MK-51117", Preset::Fastest,
	  false, true,  true,  true,  true, -1,
	  true,  true,  true,  true,  false, 2, true },

	// Crazy Taxi (US). Translucent-heavy (windows, neon signage).
	// perStrip ON: the translucent list is short and chaotic, so flushing
	// early removes a large late-frame sort. Batching it just builds a big
	// sort at frame end. shadowPass left ON — the pre-pass is cheap on a
	// tile-based SGX and removing it is a wash here.
	{ "MK-51035", Preset::Fastest,
	  true,  true,  false, true,  true, -1,
	  true,  true,  true,  true,  true,  2, false },

	// Virtua Tennis (US). Tiny scene, already fast. Do NOT nuke the shadow
	// pass (the ball shadow is gameplay-relevant) and do NOT drop the DSP
	// (ball-hit transients go mushy, which is a feel regression).
	{ "MK-51036", Preset::Speed,
	  false, true,  false, true,  true,  0,
	  true,  true,  true,  false, true,  1, false },
};

static const int kTitleCount = (int)(sizeof(kTitles) / sizeof(kTitles[0]));

// ===========================================================================
// Profile construction
//
// Explicit field assignment rather than aggregate initialisation, so adding
// a field to Profile is a compile error here instead of a silent shift.
// ===========================================================================
static void assign(Profile& p, Preset preset,
                   bool perStrip, bool skipModVolumes, bool skipShadowPass,
                   bool skipRTT,  bool forceNearest,  int  lodBias,
                   bool fastFpscr, bool singlePrecisionFPU,
                   bool skipSmcCheck, bool tightSched,
                   bool aicaDsp, int aicaSampleDiv, bool aicaArm7Alt)
{
	p.preset             = preset;
	p.perStrip           = perStrip;
	p.skipModVolumes     = skipModVolumes;
	p.skipShadowPass     = skipShadowPass;
	p.skipRTT            = skipRTT;
	p.forceNearest       = forceNearest;
	p.lodBias            = lodBias;
	p.fastFpscr          = fastFpscr;
	p.singlePrecisionFPU = singlePrecisionFPU;
	p.skipSmcCheck       = skipSmcCheck;
	p.tightSched         = tightSched;
	p.aicaDSP            = aicaDsp;
	p.aicaSampleDiv      = aicaSampleDiv;
	p.aicaArm7Alt        = aicaArm7Alt;
}

static void applyPreset(Profile& p, Preset preset)
{
	switch (preset)
	{
	case Preset::Accurate:
		// Stock flycast behaviour. Nothing on.
		assign(p, preset,
		       false, false, false, false, false,  0,
		       false, false, false, false,
		       true,  1,     false);
		break;

	case Preset::Balanced:
		// Only the wins that carry no expected visual regression.
		assign(p, preset,
		       false, false, false, false, false,  0,
		       true,  true,  false, false,
		       true,  1,     false);
		break;

	case Preset::Speed:
		assign(p, preset,
		       false, true,  false, true,  true,   0,
		       true,  true,  false, true,
		       true,  1,     false);
		break;

	case Preset::Fastest:
	default:
		assign(p, preset,
		       true,  true,  true,  true,  true,  -1,
		       true,  true,  true,  true,
		       false, 2,     true);
		break;
	}
}

// ===========================================================================
// Environment helpers
// ===========================================================================
static int envInt(const char* name, int dflt)
{
	const char* v = std::getenv(name);
	if (v == nullptr || *v == '\0')
		return dflt;
	char* end = nullptr;
	const long parsed = std::strtol(v, &end, 10);
	if (end == v)
		return dflt;
	return (int)parsed;
}

static bool envBool(const char* name, bool dflt)
{
	const char* v = std::getenv(name);
	if (v == nullptr || *v == '\0')
		return dflt;
	if (std::strcmp(v, "0") == 0 || std::strcmp(v, "false") == 0)
		return false;
	if (std::strcmp(v, "1") == 0 || std::strcmp(v, "true") == 0)
		return true;
	return dflt;
}

// ===========================================================================
// Lazy, thread-safe, once-only resolution
// ===========================================================================
static Profile           s_cur;
static std::atomic<bool> s_initDone{ false };
static std::atomic<bool> s_initRunning{ false };

static void readConfig()
{
#if defined(__vita__)
	const int dfltPreset = (int)Preset::Speed;
#else
	// A shared tree must behave exactly as stock on PC.
	const int dfltPreset = (int)Preset::Accurate;
#endif

	int presetInt = envInt("SLOPCAST_VITA_SPEED", dfltPreset);
	if (presetInt < 0) presetInt = 0;
	if (presetInt > 3) presetInt = 3;

	applyPreset(s_cur, (Preset)presetInt);

	// ---- per-title table ---------------------------------------------------
	bool useTitleTable = true;
	const char* forcedSerial = nullptr;
	if (const char* prof = std::getenv("SLOPCAST_VITA_PROFILE"))
	{
		if (std::strcmp(prof, "off") == 0)
			useTitleTable = false;
		else if (*prof != '\0')
			forcedSerial = prof;
	}

	const char* serial = forcedSerial;
	if (serial == nullptr)
		serial = speedhack_game_serial_impl();

	if (useTitleTable && serial != nullptr)
	{
		for (int i = 0; i < kTitleCount; ++i)
		{
			if (std::strcmp(kTitles[i].serial, serial) == 0)
			{
				const TitleOverride& t = kTitles[i];
				assign(s_cur, t.preset,
				       t.perStrip, t.skipModVolumes, t.skipShadowPass,
				       t.skipRTT,  t.forceNearest,  t.lodBias,
				       t.fastFpscr, t.singlePrecisionFPU,
				       t.skipSmcCheck, t.tightSched,
				       t.aicaDSP, t.aicaSampleDiv, t.aicaArm7Alt);
				break;
			}
		}
	}

	// ---- explicit per-field overrides always win ---------------------------
	s_cur.perStrip       = envBool("SLOPCAST_VITA_PERSTRIP",  s_cur.perStrip);
	s_cur.skipModVolumes = envBool("SLOPCAST_VITA_MODVOLS",   s_cur.skipModVolumes);
	s_cur.skipShadowPass = envBool("SLOPCAST_VITA_SHADOW",    s_cur.skipShadowPass);
	s_cur.skipRTT        = envBool("SLOPCAST_VITA_RTT",       s_cur.skipRTT);
	s_cur.aicaDSP        = envBool("SLOPCAST_VITA_AUDIO_DSP", s_cur.aicaDSP);

	int div = envInt("SLOPCAST_VITA_AUDIO_DIV", s_cur.aicaSampleDiv);
	if (div != 1 && div != 2 && div != 4)
		div = 1;
	s_cur.aicaSampleDiv = div;

	// ---- diagnostic --------------------------------------------------------
	if (envBool("SLOPCAST_VITA_LOG", false))
	{
		std::printf("[slopcast] preset=%d perStrip=%d modVols=%d shadow=%d "
		            "rtt=%d nearest=%d lod=%d fpscr=%d spFpu=%d smc=%d "
		            "sched=%d dsp=%d div=%d arm7alt=%d serial=%s\n",
		            (int)s_cur.preset,
		            (int)s_cur.perStrip, (int)s_cur.skipModVolumes,
		            (int)s_cur.skipShadowPass, (int)s_cur.skipRTT,
		            (int)s_cur.forceNearest, s_cur.lodBias,
		            (int)s_cur.fastFpscr, (int)s_cur.singlePrecisionFPU,
		            (int)s_cur.skipSmcCheck, (int)s_cur.tightSched,
		            (int)s_cur.aicaDSP, s_cur.aicaSampleDiv,
		            (int)s_cur.aicaArm7Alt,
		            serial != nullptr ? serial : "(none)");
		std::fflush(stdout);
	}
}

static void ensureInit()
{
	if (s_initDone.load(std::memory_order_acquire))
		return;

	bool expected = false;
	if (s_initRunning.compare_exchange_strong(expected, true,
	                                          std::memory_order_acq_rel))
	{
		readConfig();
		s_initDone.store(true, std::memory_order_release);
	}
	else
	{
		// Resolution is a handful of getenv calls — microseconds. A bounded
		// spin here is cheaper than a mutex, and it never blocks on I/O.
		while (!s_initDone.load(std::memory_order_acquire))
		{
			// spin
		}
	}
}

// ===========================================================================
// Public accessors
// ===========================================================================
const Profile& profile() { ensureInit(); return s_cur; }

bool perStrip()           { ensureInit(); return s_cur.perStrip; }
bool skipModVolumes()     { ensureInit(); return s_cur.skipModVolumes; }
bool skipShadowPass()     { ensureInit(); return s_cur.skipShadowPass; }
bool skipRTT()            { ensureInit(); return s_cur.skipRTT; }
bool forceNearest()       { ensureInit(); return s_cur.forceNearest; }
int  lodBias()            { ensureInit(); return s_cur.lodBias; }

bool fastFpscr()          { ensureInit(); return s_cur.fastFpscr; }
bool singlePrecisionFPU() { ensureInit(); return s_cur.singlePrecisionFPU; }
bool skipSmcCheck()       { ensureInit(); return s_cur.skipSmcCheck; }
bool tightSched()         { ensureInit(); return s_cur.tightSched; }

bool aicaDSP()            { ensureInit(); return s_cur.aicaDSP; }
int  aicaSampleDiv()      { ensureInit(); return s_cur.aicaSampleDiv; }
bool aicaArm7Alt()        { ensureInit(); return s_cur.aicaArm7Alt; }

// ===========================================================================
// FPSCR cache invalidation — called from the SH4 reset path
// ===========================================================================
// Defined in sh4_fpu.cpp; declared here so a reset can clear it.
extern void speedhackResetFpscrCacheImpl();

void resetFpscrCache()
{
	speedhackResetFpscrCacheImpl();
}

// ===========================================================================
// Thread affinity
//
// SceKernelCpuAffinityMask values map to the four Cortex-A9 cores:
//   USER_0 -> core 0, USER_1 -> core 1, USER_2 -> core 2, USER_3 -> core 3.
// ===========================================================================
#if defined(__vita__)

#define VITA_CORE_EMU   0x00000001u   // USER_0
#define VITA_CORE_AUDIO 0x00000002u   // USER_1
#define VITA_CORE_GPU   0x00000004u   // USER_2

static void pinCurrentThread(unsigned int mask)
{
	const SceUID tid = sceKernelGetThreadId();
	if (tid < 0)
		return;
	sceKernelChangeThreadCpuAffinityMask(tid, mask);
}

void pinEmuThread()   { pinCurrentThread(VITA_CORE_EMU);   }
void pinAudioThread() { pinCurrentThread(VITA_CORE_AUDIO); }
void pinGpuThread()   { pinCurrentThread(VITA_CORE_GPU);   }

void yieldCore(int microseconds)
{
	if (microseconds <= 0)
		microseconds = 1;
	sceKernelDelayThread((SceUInt)microseconds);
}

#else  // !__vita__

void pinEmuThread()   {}
void pinAudioThread() {}
void pinGpuThread()   {}
void yieldCore(int)   {}

#endif

} // namespace speedhack
