/* Lip sync from the audio itself (no text, no phoneme table).
   Each frame the spectrum of the voice that is actually playing is split into the bands that carry the vowel colour:
     F1 (250-900 Hz)  strong for open vowels  -> the jaw drops
     F2 (900-2600 Hz) strong for front vowels (ee, eh) -> lips spread; weak for back vowels (oo, oh) -> lips round
     > 4.5 kHz        strong for s, sh, f      -> teeth show, jaw nearly shut
     silence between sounds                    -> lips come together (m, b, p)
   This is the usual formant approximation used for real-time mouth animation. It follows the sound, so it cannot drift
   out of sync, but it estimates shapes from acoustics, it does not know which letter was said. */
(function (root) {
  "use strict";

  function Analyzer() {
    this.peak = 1e-6; this.prev = 0; this.r2m = 0.3;   // r2m: this voice's usual front-vowel share
    this.s = { open: 0, wide: 0, round: 0, teeth: 0, loud: 0, emph: 0 };
  }

  /* db: Float32Array from AnalyserNode.getFloatFrequencyData (decibels per bin) */
  Analyzer.prototype.step = function (db, sampleRate, fftSize, dt) {
    const binHz = sampleRate / fftSize;
    const band = (lo, hi) => {
      let p = 0;
      const a = Math.max(1, Math.floor(lo / binHz)), b = Math.min(db.length - 1, Math.ceil(hi / binHz));
      for (let i = a; i <= b; i++) p += Math.pow(10, db[i] / 10);
      return p;
    };
    const low = band(80, 250), f1 = band(250, 900), f2 = band(900, 2600), f3 = band(2600, 4500), hiss = band(4500, 9500);
    const level = Math.sqrt(low + f1 + f2 + f3 + hiss);

    this.peak = Math.max(level, this.peak * Math.pow(0.5, dt / 4));          // automatic gain: loudest recent sound = 1
    let loud = level < 1e-4 ? 0 : Math.min(1, level / this.peak);
    if (loud < 0.1) loud = 0;                                                  // gaps between sounds: lips close

    const voiced = f1 + f2 + f3 + 1e-18;
    const r1 = f1 / voiced;                                                    // open-vowel share
    const r2 = f2 / (f1 + f2 + 1e-18);                                         // front-vowel share
    const fric = hiss / (f2 + f3 + hiss + 1e-18);                              // s / sh / f share

    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const fricative = fric > 0.45;
    // spread vs round is judged against this speaker's own average, so a deep or a bright voice both look right
    if (loud > 0.25 && !fricative) this.r2m += (r2 - this.r2m) * (1 - Math.exp(-dt / 1.5));
    const dev = r2 - this.r2m;
    const t = {
      open: fricative ? 0.12 * loud : clamp(Math.pow(loud, 0.75) * (0.45 + 0.9 * r1), 0, 1),
      wide: clamp(dev * 3.2, -0.6, 0.9) * (loud > 0 ? 1 : 0),
      round: clamp(-dev * 3.2, 0, 1) * loud,
      teeth: clamp(fric * 1.6, 0, 1) * Math.min(1, loud * 2.5),
      loud,
    };

    const s = this.s;
    for (const k of ["open", "wide", "round", "teeth", "loud"]) {
      const attack = k === "open" ? 0.035 : 0.06, release = k === "open" ? 0.09 : 0.12;
      s[k] += (t[k] - s[k]) * (1 - Math.exp(-dt / (t[k] > s[k] ? attack : release)));
    }
    s.emph += (Math.max(0, loud - this.prev) / Math.max(dt, 0.008) * 0.05 - s.emph) * (1 - Math.exp(-dt / 0.12));  // stress: used to nod / lift brows
    this.prev = loud;
    return s;
  };

  Analyzer.prototype.reset = function () { this.s = { open: 0, wide: 0, round: 0, teeth: 0, loud: 0, emph: 0 }; this.prev = 0; };

  if (typeof module !== "undefined" && module.exports) module.exports = Analyzer; else root.LipSync = Analyzer;
})(typeof window !== "undefined" ? window : globalThis);
