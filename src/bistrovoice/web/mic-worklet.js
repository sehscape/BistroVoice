// Runs on the audio thread. Turns the microphone (usually 48 kHz) into
// 16 kHz mono PCM16 frames of 20 ms, plus the frame's loudness (RMS) so the
// main thread can tell speech from silence.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.phase = 0;
    this.acc = 0;
    this.count = 0;
    this.frame = new Int16Array(320);
    this.k = 0;
    this.energy = 0;
  }

  process(inputs) {
    const x = inputs[0] && inputs[0][0];
    if (!x) return true;
    for (let i = 0; i < x.length; i++) {
      // Box-filter decimation: average the input samples that fall into each
      // output sample. Crude, but plenty for speech recognition.
      this.acc += x[i];
      this.count++;
      this.phase++;
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio;
        const s = Math.max(-1, Math.min(1, this.acc / this.count));
        this.acc = 0;
        this.count = 0;
        this.frame[this.k++] = s * 32767;
        this.energy += s * s;
        if (this.k === this.frame.length) {
          const rms = Math.sqrt(this.energy / this.frame.length);
          this.port.postMessage({ pcm: this.frame.buffer, rms }, [this.frame.buffer]);
          this.frame = new Int16Array(320);
          this.k = 0;
          this.energy = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
