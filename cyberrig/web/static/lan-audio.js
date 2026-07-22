/**
 * CyberRig LAN RX audio — plays FTDX10 USB audio streamed from the shack PC.
 *
 * Server: /ws/audio
 *   hello (JSON text): { sample_rate, format:"s16le", channels:1 }
 *   binary frames: int16 little-endian mono PCM @ sample_rate
 */
(function (global) {
  class LanAudioPlayer {
    constructor() {
      this.ws = null;
      this.ctx = null;
      this.nextTime = 0;
      this.playing = false;
      this.sr = 12000;
      this.gainNode = null;
      this._onStatus = null;
      this._queueSecs = 0;
    }

    onStatus(fn) { this._onStatus = fn; }
    _status(msg, cls) {
      if (this._onStatus) this._onStatus(msg, cls);
    }

    get isPlaying() { return this.playing; }

    async start() {
      if (this.playing) return;
      // User gesture required for AudioContext
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') await this.ctx.resume();
      this.gainNode = this.ctx.createGain();
      this.gainNode.gain.value = 1.0;
      this.gainNode.connect(this.ctx.destination);
      this.nextTime = 0;
      this._queueSecs = 0;

      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      this.ws = new WebSocket(`${proto}//${location.host}/ws/audio`);
      this.ws.binaryType = 'arraybuffer';
      this.playing = true;
      this._status('Connecting audio…', '');

      this.ws.onopen = () => this._status('Audio connected…', 'live');
      this.ws.onerror = () => this._status('Audio WebSocket error', 'err');
      this.ws.onclose = () => {
        this.playing = false;
        this._status('Audio stopped', '');
        this._teardownCtx();
      };
      this.ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'hello') {
              if (msg.ok === false) {
                this._status(msg.error || 'Audio capture failed', 'err');
                this.stop();
                return;
              }
              this.sr = msg.sample_rate || 12000;
              this._status('Listening · ' + (msg.device_name || 'RX'), 'live');
            } else if (msg.type === 'gain') {
              // server ack
            }
          } catch (_) {}
          return;
        }
        // Binary PCM s16le
        this._enqueuePcm(ev.data);
      };
    }

    stop() {
      this.playing = false;
      if (this.ws) {
        try { this.ws.close(); } catch (_) {}
        this.ws = null;
      }
      this._teardownCtx();
      this._status('Audio stopped', '');
    }

    setGain(g) {
      g = Math.max(0.05, Math.min(4, +g || 1));
      if (this.gainNode) this.gainNode.gain.value = g;
      if (this.ws && this.ws.readyState === 1) {
        this.ws.send(JSON.stringify({ type: 'gain', value: g }));
      }
    }

    toggle() {
      if (this.playing) this.stop();
      else this.start();
    }

    _teardownCtx() {
      if (this.ctx) {
        try { this.ctx.close(); } catch (_) {}
        this.ctx = null;
        this.gainNode = null;
      }
      this.nextTime = 0;
      this._queueSecs = 0;
    }

    _enqueuePcm(arrayBuffer) {
      if (!this.ctx || !this.playing) return;
      const i16 = new Int16Array(arrayBuffer);
      if (!i16.length) return;
      const f32 = new Float32Array(i16.length);
      for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;

      const buf = this.ctx.createBuffer(1, f32.length, this.sr);
      buf.copyToChannel(f32, 0);

      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.gainNode || this.ctx.destination);

      const now = this.ctx.currentTime;
      // Keep ~80–200 ms ahead; drop if we fall way behind
      if (this.nextTime < now + 0.05) this.nextTime = now + 0.08;
      if (this.nextTime > now + 0.5) {
        // Over-buffered — skip this chunk to catch up
        return;
      }
      src.start(this.nextTime);
      this.nextTime += buf.duration;
    }
  }

  global.LanAudioPlayer = LanAudioPlayer;
})(window);
