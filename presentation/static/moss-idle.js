// Display choreography only. All angles are radians, time is in active seconds.
// Frame columns: progress, body yaw, body pitch, arm counter-swivel.
const clip = (id, label, seconds, description, frames) => Object.freeze({
  id, label, seconds: Object.freeze(seconds), description,
  frames: Object.freeze(frames.map(frame => Object.freeze(frame))),
});

export const MOSS_IDLE_CLIPS = Object.freeze([
  clip('panorama', '全景巡望', [18, 30], '慢慢扫过左右两侧，在远处稍作停留。', [
    [0,0,0,0], [.15,-.44,-.04,-.035], [.27,-.44,-.04,-.035],
    [.52,.08,.025,.015], [.74,.46,-.035,.04], [.85,.46,-.035,.04], [1,0,0,0],
  ]),
  clip('inspect', '定点观察', [10, 20], '锁定一处，俯仰确认，再认真看一眼。', [
    [0,0,0,0], [.17,.34,.025,.045], [.34,.34,.025,.045], [.44,.37,.12,.065],
    [.57,.37,.12,.065], [.67,.32,-.055,.04], [.83,.32,-.055,.04], [1,0,0,0],
  ]),
  clip('look-up', '抬头探看', [8, 16], '像听见了上方的动静，抬头左右寻找。', [
    [0,0,0,0], [.2,-.18,-.18,-.04], [.38,-.18,-.18,-.04],
    [.62,.29,-.16,.04], [.78,.29,-.16,.04], [1,0,0,0],
  ]),
  clip('look-down', '低头巡视', [10, 18], '低头扫过近处，沿边缘慢慢确认。', [
    [0,0,0,0], [.18,-.35,.16,-.05], [.32,-.35,.16,-.05], [.66,.38,.18,.055],
    [.78,.38,.18,.055], [.89,.08,.08,.02], [1,0,0,0],
  ]),
  clip('listen', '侧向倾听', [8, 14], '朝一侧安静地听，轻调机械臂，随后回正。', [
    [0,0,0,0], [.18,.4,-.045,.06], [.37,.4,-.045,.06], [.46,.36,.035,.08],
    [.57,.36,.035,.08], [.68,.41,-.02,.055], [.83,.41,-.02,.055], [1,0,0,0],
  ]),
  clip('double-take', '好奇回看', [6, 11], '看过之后又回头：刚才是不是有什么？', [
    [0,0,0,0], [.18,.26,.015,.025], [.31,.26,.015,.025], [.43,-.045,0,-.01],
    [.53,.48,-.09,.07], [.72,.48,-.09,.07], [.81,.41,.04,.035], [1,0,0,0],
  ]),
  clip('nod', '点头认可', [5, 9], '先轻点一下，再认真地点头确认。', [
    [0,0,0,0], [.17,.1,-.065,.02], [.29,.1,.13,-.025], [.4,.1,-.045,.015],
    [.54,.1,.18,-.035], [.66,.1,-.025,.015], [.81,.08,.02,0], [1,0,0,0],
  ]),
  clip('ponder', '思考迟疑', [12, 24], '停一停，换个角度想想，再回到正面。', [
    [0,0,0,0], [.17,-.24,-.08,-.035], [.34,-.24,-.08,-.035], [.49,-.12,.11,.02],
    [.64,-.12,.11,.02], [.78,.19,.035,.045], [.87,.19,.035,.045], [1,0,0,0],
  ]),
  clip('fluster', '短暂慌张', [5, 9], '左右确认得有点急，然后努力恢复镇定。', [
    [0,0,0,0], [.12,-.28,-.06,-.055], [.23,.33,-.035,.07], [.34,-.38,.025,-.075],
    [.45,.3,-.055,.065], [.56,-.19,.09,-.04], [.68,.1,-.025,.02], [.83,0,.025,0], [1,0,0,0],
  ]),
  clip('sneeze', '模拟喷嚏', [5, 8], '微微后仰蓄势，啊嚏两下，整理好姿态。', [
    [0,0,0,0], [.17,.035,-.1,.035], [.31,-.025,-.17,.065], [.39,.025,-.12,.05],
    [.445,-.025,.19,-.085], [.515,.015,-.095,.05], [.565,-.015,.16,-.07],
    [.65,.025,-.025,.025], [.73,-.02,.045,-.015], [.84,.01,0,.005], [1,0,0,0],
  ]),
]);

const neutral = () => ({yaw: 0, pitch: 0, arm: 0});
const smooth = t => t*t*t*(t*(t*6-15)+10);

export function sampleIdle(clip, progress, mirror = 1, gain = 1) {
  const t = Math.max(0, Math.min(1, progress));
  const frames = clip.frames;
  let i = 1;
  while (i < frames.length-1 && t > frames[i][0]) i++;
  const a = frames[i-1], b = frames[i], blend = smooth((t-a[0])/(b[0]-a[0]));
  const value = column => (a[column]+(b[column]-a[column])*blend)*gain;
  return {yaw: value(1)*mirror, pitch: value(2), arm: value(3)*mirror};
}

export class MossIdlePlayer {
  constructor(random = Math.random) {
    this.random = random; this.bag = []; this.lastId = null; this.current = null;
  }

  play(id) {
    let chosen;
    if (id) {
      chosen = MOSS_IDLE_CLIPS.find(clip => clip.id === id);
      if (!chosen) throw new Error('未知的 MOSS 待机动作');
    } else {
      if (!this.bag.length) {
        this.bag = [...MOSS_IDLE_CLIPS];
        for (let i = this.bag.length-1; i > 0; i--) {
          const j = Math.floor(this.random()*(i+1));
          [this.bag[i],this.bag[j]] = [this.bag[j],this.bag[i]];
        }
        if (this.bag.at(-1).id === this.lastId) [this.bag[0],this.bag[this.bag.length-1]] = [this.bag.at(-1),this.bag[0]];
      }
      chosen = this.bag.pop();
    }
    this.lastId = chosen.id;
    this.current = {clip: chosen, elapsed: 0,
      duration: chosen.seconds[0]+this.random()*(chosen.seconds[1]-chosen.seconds[0]),
      mirror: this.random() < .5 ? -1 : 1, gain: .9+this.random()*.1};
    return this.current;
  }

  advance(seconds, {loop = true} = {}) {
    if (!this.current && loop) this.play();
    let remaining = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
    while (this.current) {
      const rest = this.current.duration-this.current.elapsed;
      if (remaining < rest) {this.current.elapsed += remaining; break;}
      remaining -= rest;
      if (!loop) {this.reset(); return neutral();}
      this.play();
    }
    const value = this.current;
    return value ? sampleIdle(value.clip, value.elapsed/value.duration, value.mirror, value.gain) : neutral();
  }

  reset() {this.current = null;}
}
