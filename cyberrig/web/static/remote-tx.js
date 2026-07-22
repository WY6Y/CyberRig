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
      this.gain = 1.6; // hotter default — USB RPORT often needs drive
      this._onStatus = null;
      this._connected = false;
      this._phase = 0;
    }

    onStatus(fn) { this._onStatus = fn; }
    _status(msg, cls) {
      if (this._onStatus) this._onStatus(msg, cls || '', {
        armed: this.armed, ptt: this.ptt, connected: this._connected,
      });
    }

    get isArmed() { return this.armed; }
    get isPtt() { return this.ptt; }

    async arm() {
      if (this.armed) return true;
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
            channelCount: 1,
          },
          video: false,
        });
      } catch (e) {
        this._status('Mic permission denied: ' + e.message, 'err');
        return false;
      }

      this.ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
      this.sr = this.ctx.sampleRate || 48000;
      if (this.ctx.state === 'suspended') await this.ctx.resume();

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

      // Mic → processor → (silent local) destination so Chrome keeps the graph alive
      this.src = this.ctx.createMediaStreamSource(this.stream);
      const bufferSize = 2048;
      this.proc = this.ctx.createScriptProcessor(bufferSize, 1, 1);
      this.proc.onaudioprocess = (e) => {
        if (!this.ptt || !this.ws || this.ws.readyState !== 1) return;
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
      this.src.connect(this.proc);
      this.proc.connect(this.mute);
      this.mute.connect(this.ctx.destination);

      this.armed = true;
      return true;
    }

    disarm() {
      this.pttDown(false);
      this.armed = false;
      this._connected = false;
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
      if (this.stream) {
        this.stream.getTracks().forEach((t) => t.stop());
        this.stream = null;
      }
      if (this.ctx) {
        try { this.ctx.close(); } catch (_) {}
        this.ctx = null;
      }
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
      if (on === this.ptt) return;
      this.ptt = on;
      if (this.ws && this.ws.readyState === 1) {
        // Clean key — no pilot beeps (path is proven; beeps were for debug)
        try {
          this.ws.send(JSON.stringify({ type: 'ptt', on }));
        } catch (_) {}
      }
      if (!on) this._localUnkey();
      this._status(
        on ? '● TX — speak now' : (this.armed ? 'Armed — hold PTT' : 'Remote TX off'),
        on ? 'tx' : (this.armed ? 'live' : '')
      );
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
