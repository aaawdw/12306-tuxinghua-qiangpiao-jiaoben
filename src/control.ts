export class Stopped extends Error { constructor() { super('已停止，浏览器保留供人工处理。'); } }
export class Handoff extends Error {}
export class Control {
  paused = false;
  stopped = false;
  async checkpoint(): Promise<void> {
    while (this.paused && !this.stopped) await new Promise(r => setTimeout(r, 100));
    if (this.stopped) throw new Stopped();
  }
  async sleep(ms: number): Promise<void> {
    const end = Date.now() + ms;
    do { await this.checkpoint(); await new Promise(r => setTimeout(r, Math.min(100, Math.max(0, end - Date.now())))); } while (Date.now() < end);
    await this.checkpoint();
  }
}
