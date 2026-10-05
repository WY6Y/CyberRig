/**
 * Remote SSB TX — browser mic → LAN → Yaesu USB audio out + CAT PTT.
 *
 * Server: /ws/tx
 *   client binary: s16le mono PCM  (must be a tight slice of the buffer)
 *   client text: {type:"ptt", on:true|false}, {type:"hello", sample_rate}
 *   client text: {type:"beep", ms:1500} → server-side test tone while keyed
 */
(function (global) {
  function pcmToArrayBuffer(i16) {
    // Critical: don't send the whole underlying ArrayBuffer (can be huge/shared)
    return i16.buffer.slice(i16.byteOffset, i16.byteOffset + i16.byteLength);
  }

  function _resyncRxAudio() {
    // The mic route change knocks the LISTEN player's scheduling clock out of
    // step. Re-anchor it now instead of letting it drop and stack chunks.
    try { if (global._lanAudio) global._lanAudio.resync(); } catch (_) {}
  }

  class RemoteTx {
    constructor() {
      this.ws = null;
      this.ctx = null;
      this.proc = null;
      this.src = null;
      this.stream = null;
      this.mute = null;
      this.armed = false;
      this.ptt = false;
      this.sr = 48000;
      this.gain = 0.65; // profile default; AirPods are much hotter than the shack mic
      this._onStatus = null;
      this._connected = false;
      this._phase = 0;
      this._sharedCtx = false;
      this._pttGen = 0;
      this._keyed = false;   // radio actually keyed (set on first mic frame)
      this._keyWatchdog = null;
      this.profile = null;   // active TX profile: optional mic / hpf / lpf
      this._micId = null;    // deviceId resolved from profile.mic
      this._filters = [];
    }

    setProfile(p) {
      const prev = this.profile && this.profile.mic;
      this.profile = p || null;
      if ((p && p.mic) !== prev) this._micId = null;
      // Takes effect on the next over: the graph is rebuilt on every PTT.
    }

    // Pin the profile's mic by label substring. Labels only exist once mic
    // permission is granted, which arm()'s probe has done. No match (the
    // webcam profile picked on an iPhone) falls back to the default input.
    async _resolveMic() {
      const want = this.profile && this.profile.mic;
      if (!want || this._micId) return;
      try {
        const devs = await navigator.mediaDevices.enumerateDevices();
        const hit = devs.find((d) => d.kind === 'audioinput' &&
          (d.label || '').toLowerCase().includes(String(want).toLowerCase()));
        this._micId = hit ? hit.deviceId : null;
        if (!hit) this._status('Mic "' + want + '" not found — using default input', 'err');
      } catch (_) {
        this._micId = null;
      }
    }

    onStatus(fn) { this._onStatus = fn; }
    _status(msg, cls) {
      if (this._onStatus) this._onStatus(msg, cls || '', {
        armed: this.armed, ptt: this.ptt, connected: this._connected,
      });
    }

    get isArmed() { return this.armed; }
    get isPtt() { return this.ptt; }

    _setSession(type) {
      // Safari: LISTEN is `playback` (full volume). Mic capture switches the
      // page to play-and-record, which ducks other audio — that is why RX
      // went quiet the moment REMOTE TX was armed. Keep playback until PTT.
      try {
        if (navigator.audioSession) navigator.audioSession.type = type;
      } catch (_) {}
    }

    _micConstraints() {
      return {
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1,
          ...(this._micId ? { deviceId: { exact: this._micId } } : {}),
        },
        video: false,
      };
    }

    _ensureCtx() {
      if (this.ctx) return;
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC({ sampleRate: 48000 });
      this._sharedCtx = false;
      this.sr = this.ctx.sampleRate || 48000;
    }

    async _ensureMicGraph() {
      if (this.stream && this.proc && this.stream.getTracks().some((t) => t.readyState === 'live')) {
        return;
      }
      this._releaseMicGraph();
      this._setSession('play-and-record');
      await this._resolveMic();
      try {
        this.stream = await navigator.mediaDevices.getUserMedia(this._micConstraints());
      } catch (e) {
        if (!this._micId) throw e;
        this._micId = null;   // pinned mic unplugged: take the default
        this.stream = await navigator.mediaDevices.getUserMedia(this._micConstraints());
      }
      this._ensureCtx();
      if (this.ctx.state === 'suspended') await this.ctx.resume();
      if (this.ws && this.ws.readyState === 1) {
        try { this.ws.send(JSON.stringify({ type: 'hello', sample_rate: this.sr })); } catch (_) {}
      }
      this.src = this.ctx.createMediaStreamSource(this.stream);
      const bufferSize = 2048;
      this.proc = this.ctx.createScriptProcessor(bufferSize, 1, 1);
      this.proc.onaudioprocess = (e) => {
        if (!this.ptt || !this.ws || this.ws.readyState !== 1) return;
        // Key on the FIRST frame we can actually send, not before the mic
        // exists. See _beginKey for why this moved here.
        if (!this._keyed) {
          this._keyed = true;
          if (this._keyWatchdog) { clearTimeout(this._keyWatchdog); this._keyWatchdog = null; }
          this._sendPtt(true);
          this._status('● TX — speak now', 'tx');
        }
        const input = e.inputBuffer.getChannelData(0);
        const i16 = new Int16Array(input.length);
        const g = this.gain;
        for (let i = 0; i < input.length; i++) {
          let s = input[i] * g;
          if (s > 1) s = 1;
          if (s < -1) s = -1;
          i16[i] = (s * 32767) | 0;
        }
        try {
          this.ws.send(pcmToArrayBuffer(i16));
        } catch (_) {}
      };
      this.mute = this.ctx.createGain();
      this.mute.gain.value = 0;
      // Profile band-limit, ahead of the gain in onaudioprocess. HPF is two
      // 2nd-order stages (24 dB/oct) so the room rumble is gone, not dented.
      let tail = this.src;
      const p = this.profile || {};
      const stage = (type, hz) => {
        const f = this.ctx.createBiquadFilter();
        f.type = type;
        f.frequency.value = hz;
        f.Q.value = 0.707;
        tail.connect(f);
        tail = f;
        this._filters.push(f);
      };
      if (+p.hpf > 0) { stage('highpass', +p.hpf); stage('highpass', +p.hpf); }
      if (+p.lpf > 0) stage('lowpass', +p.lpf);
      tail.connect(this.proc);
      this.proc.connect(this.mute);
      this.mute.connect(this.ctx.destination);
    }

    // Keeping the capture alive past unkey was tried (1.5 s) and reverted:
    // Safari stays in play-and-record while a track is live, so RX audio stayed
    // ducked, and releasing later added a SECOND route change — the RX dropped
    // in and out for a second or two after every over. One transition, at unkey.
    _releaseMicGraph() {
      if (this.proc) {
        try { this.proc.disconnect(); } catch (_) {}
        this.proc.onaudioprocess = null;
        this.proc = null;
      }
      if (this.src) {
        try { this.src.disconnect(); } catch (_) {}
        this.src = null;
      }
      if (this.mute) {
        try { this.mute.disconnect(); } catch (_) {}
        this.mute = null;
      }
      this._filters.forEach((f) => { try { f.disconnect(); } catch (_) {} });
      this._filters = [];
      if (this.stream) {
        this.stream.getTracks().forEach((t) => t.stop());
        this.stream = null;
      }
      this._setSession('playback');
    }

    _sendPtt(on) {
      if (this.ws && this.ws.readyState === 1) {
        try { this.ws.send(JSON.stringify({ type: 'ptt', on: !!on })); } catch (_) {}
      }
    }

    async arm() {
      if (this.armed) return true;
      // Ask for mic now so the first PTT is not a permission prompt, then
      // drop the capture immediately so LISTEN is not ducked while armed.
      // navigator.mediaDevices is simply absent outside a secure context --
      // which on iOS Safari includes an HTTPS page whose certificate was
      // clicked through, exactly what `tls internal` produces without the
      // reverse proxy's root CA installed. RX websockets keep working in that state,
      // so the page looks healthy and only the mic is dead. Say so plainly:
      // "permission denied" sent the operator hunting a Safari setting that was
      // already correct (2026-08-28).
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this._status(
          'No mic API — page is not a secure context (isSecureContext=' +
            (window.isSecureContext ? 'true' : 'false') + ', ' + location.protocol +
            '). Trust the site certificate, then reload.',
          'err'
        );
        return false;
      }
      // The session MUST be play-and-record across the probe. Setting
      // 'playback' first (which is what we want while merely armed, so LISTEN
      // is not ducked) makes iOS Safari reject capture outright:
      // "InvalidStateError: AudioSession category is not compatible with audio
      // capture" -- the arm silently turned itself back off on the iPhone
      // while FreeDV, which sets play-and-record before getUserMedia, worked
      // fine from the same page (found 2026-08-28). Restore 'playback' as soon
      // as the probe tracks are stopped; PTT re-enters play-and-record via
      // _ensureMicGraph().
      this._setSession('play-and-record');
      try {
        const probe = await navigator.mediaDevices.getUserMedia(this._micConstraints());
        probe.getTracks().forEach((t) => t.stop());
        _resyncRxAudio(); // the probe moved the route too
      } catch (e) {
        this._status(
          'Mic blocked: ' + (e.name || 'error') + ' — ' + (e.message || ''),
          'err'
        );
        return false;
      } finally {
        this._setSession('playback');
      }

      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      this.ws = new WebSocket(`${proto}//${location.host}/ws/tx`);
      this.ws.binaryType = 'arraybuffer';

      await new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error('TX WS timeout')), 10000);
        this.ws.onerror = () => { clearTimeout(t); reject(new Error('TX WS error')); };
        this.ws.onmessage = (ev) => {
          if (typeof ev.data !== 'string') return;
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'hello') {
              clearTimeout(t);
              if (!msg.ok) {
                reject(new Error(msg.error || 'TX audio open failed'));
                return;
              }
              this._connected = true;
              const mod = msg.mod_setup
                ? ` MOD=${msg.mod_setup.ssb_mod_source}/${msg.mod_setup.ssb_rear_select}`
                : '';
              this._status(
                'Armed · ' + (msg.device_name || 'USB out') + mod + ' — HOLD PTT',
                'live'
              );
              if (msg.profile) this.setProfile(msg.profile);
              if (msg.profile && msg.profile.gain != null) {
                this.gain = Math.max(0.1, Math.min(3.0, +msg.profile.gain));
              }
              this.ws.send(JSON.stringify({ type: 'hello', sample_rate: this.sr }));
              this.ws.send(JSON.stringify({ type: 'gain', value: this.gain }));
              resolve(msg);
            } else if (msg.type === 'ptt' && msg.ok === false) {
              this._status(msg.error || 'PTT failed', 'err');
              this.ptt = false;
            } else if (msg.type === 'ptt' && msg.on) {
              this._status('● TX — mic live (watch ALC)', 'tx');
            }
          } catch (_) {}
        };
        this.ws.onclose = () => {
          this._connected = false;
          if (this.ptt) this._localUnkey();
          this._status('TX link closed', '');
        };
      }).catch((e) => {
        this.disarm();
        this._status(String(e.message || e), 'err');
        throw e;
      });

      this.armed = true;
      return true;
    }

    disarm() {
      this.pttDown(false);
      this.armed = false;
      this._connected = false;
      this._releaseMicGraph();
      if (this.ctx && !this._sharedCtx) {
        try { this.ctx.close(); } catch (_) {}
      }
      this.ctx = null;
      this._sharedCtx = false;
      if (this.ws) {
        try {
          this.ws.send(JSON.stringify({ type: 'stop' }));
          this.ws.close();
        } catch (_) {}
        this.ws = null;
      }
      this._status('Remote TX off (MOD restored)', '');
    }

    async toggleArm() {
      if (this.armed) {
        this.disarm();
        return false;
      }
      try {
        return await this.arm();
      } catch (_) {
        return false;
      }
    }

    pttDown(on) {
      on = !!on;
      if (on && !this.armed) {
        this._status('Arm Remote TX first', 'err');
        return;
      }
      this._pttGen += 1;
      const gen = this._pttGen;
      if (on) {
        this._beginKey(gen);
        return;
      }
      this.ptt = false;
      if (this._keyWatchdog) { clearTimeout(this._keyWatchdog); this._keyWatchdog = null; }
      if (this._keyed) this._sendPtt(false);
      this._keyed = false;
      this._releaseMicGraph();
      this._localUnkey();
      _resyncRxAudio();
      this._status(this.armed ? 'Armed — hold PTT' : 'Remote TX off', this.armed ? 'live' : '');
    }

    async _beginKey(gen) {
      // Keying before the mic graph exists put dead air on the air. The mic
      // needs 200 ms to ~2 s (getUserMedia + AudioContext resume, worst on
      // iOS) and the radio used to be keyed for all of it: measured on 3916
      // (2026-09-04) a 3.1 s over delivered 0.9 s of audio (28% fill) and a
      // 2.1 s over delivered NOTHING (0% fill). On a net of short overs that
      // is most of what you say. So the radio is now keyed from the first
      // mic frame we can actually send (see onaudioprocess). The button
      // still answers instantly — it just says "keying" until that is true.
      this.ptt = true;          // gate that lets onaudioprocess send
      this._keyed = false;      // ...and its first frame keys the radio
      this._status('◌ keying…', 'tx');
      try {
        await this._ensureMicGraph();
        if (gen !== this._pttGen) {
          this._releaseMicGraph();
          return;
        }
        // A graph that never delivers a frame would sit here holding PTT
        // down and never key. Say so instead of looking armed and mute.
        if (this._keyWatchdog) clearTimeout(this._keyWatchdog);
        this._keyWatchdog = setTimeout(() => {
          this._keyWatchdog = null;
          if (gen !== this._pttGen || this._keyed) return;
          this.ptt = false;
          this._releaseMicGraph();
          this._localUnkey();
          this._status('Mic delivered no audio — not keying', 'err');
        }, 2000);
      } catch (e) {
        if (gen !== this._pttGen) return;
        // No mic means no audio to send — and now, no key either.
        this.ptt = false;
        this._localUnkey();
        this._status('Mic failed: ' + e.message, 'err');
      }
    }

    /** Explicit path test only (TEST TONE button) — not used on normal PTT */
    async testTone(ms = 2500) {
      if (!this.armed) {
        const ok = await this.arm();
        if (!ok) return false;
      }
      if (!this.ws || this.ws.readyState !== 1) return false;
      this.ptt = true;
      // Server beep command keys + holds for ms (do not double-unkey too early)
      this.ws.send(JSON.stringify({ type: 'beep', ms: ms, hz: 700, amp: 0.55, unkey: true }));
      this._status('● TEST TONE ' + (ms / 1000) + 's — watch PO/ALC on radio', 'tx');
      setTimeout(() => {
        this.ptt = false;
        this._status(this.armed ? 'Armed — HOLD PTT' : 'Remote TX off', this.armed ? 'live' : '');
      }, ms + 400);
      return true;
    }

    _localUnkey() {
      this.ptt = false;
    }

    setGain(g) {
      this.gain = Math.max(0.1, Math.min(3.0, +g || 1));
      if (this.ws && this.ws.readyState === 1) {
        this.ws.send(JSON.stringify({ type: 'gain', value: this.gain }));
      }
    }
  }

  global.RemoteTx = RemoteTx;
})(window);
