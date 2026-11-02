import * as THREE from './vendor/three.module.min.js';

const clamp = THREE.MathUtils.clamp;

export class MossRig {
  constructor(root) {
    // GLTFLoader sanitizes spaces in glTF node names into underscores.
    const find = name => root.getObjectByName(name.replaceAll(' ', '_')) || root.getObjectByName(name);
    this.ceiling = find('MOSS Ceiling Swivel');
    this.elbow = find('MOSS Elbow Swivel');
    this.wrist = find('MOSS Wrist Swivel');
    this.body = find('MOSS Body Pitch');
    this.fixed = find('MOSS Fixed Link');
    this.ready = Boolean(this.ceiling && this.elbow && this.wrist && this.body && this.fixed);
    this.yaw = 0; this.pitch = 0;
  }

  lookAt(yaw, pitch) {
    if (!this.ready) return;
    this.yaw = clamp(yaw, -.55, .55);
    this.pitch = clamp(pitch, -.20, .20);
    // A requested body heading is distributed over the three vertical joints.
    // The upper link counter-turns slightly; all children remain on their axes.
    // These are display limits, not a physical IK model or robot command.
    this.ceiling.rotation.y = -.25 * this.yaw;
    this.elbow.rotation.y = .65 * this.yaw;
    this.wrist.rotation.y = .60 * this.yaw;
    this.body.rotation.x = this.pitch;
  }
}
