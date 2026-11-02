import * as THREE from './vendor/three.module.min.js';
import {GLTFLoader} from './vendor/addons/loaders/GLTFLoader.js';
import {makeDetailedMoss} from './moss-detailed.js';

function disposeObject(object) {
  const materials = new Set(), textures = new Set(), images = new Set();
  object?.traverse(node => {
    node.geometry?.dispose();
    for (const material of (Array.isArray(node.material) ? node.material : [node.material])) {
      if (!material) continue;
      materials.add(material);
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
    }
  });
  for (const texture of textures) {if (texture.image?.close) images.add(texture.image); texture.dispose();}
  for (const image of images) image.close();
  for (const material of materials) material.dispose();
}

function roundedBox(w, h, depth, radius = .08) {
  const x = -w / 2, y = -h / 2;
  const shape = new THREE.Shape();
  shape.moveTo(x + radius, y);
  shape.lineTo(x + w - radius, y);
  shape.quadraticCurveTo(x + w, y, x + w, y + radius);
  shape.lineTo(x + w, y + h - radius);
  shape.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  shape.lineTo(x + radius, y + h);
  shape.quadraticCurveTo(x, y + h, x, y + h - radius);
  shape.lineTo(x, y + radius);
  shape.quadraticCurveTo(x, y, x + radius, y);
  const geometry = new THREE.ExtrudeGeometry(shape, {depth, bevelEnabled: true, bevelSegments: 2,
    steps: 1, bevelSize: .025, bevelThickness: .025, curveSegments: 6});
  geometry.translate(0, 0, -depth / 2);
  return geometry;
}

export function makeSimpleMoss(accent) {
  const group = new THREE.Group();
  const white = new THREE.MeshStandardMaterial({color: '#c4c9c7', roughness: .48, metalness: .38});
  const edge = new THREE.MeshStandardMaterial({color: '#a2acae', roughness: .45, metalness: .55});
  const dark = new THREE.MeshStandardMaterial({color: '#252c30', roughness: .5, metalness: .65});
  const black = new THREE.MeshStandardMaterial({color: '#080e12', roughness: .24, metalness: .55});
  const lens = new THREE.MeshPhysicalMaterial({color: '#2b0809', roughness: .12, metalness: .35,
    clearcoat: 1, clearcoatRoughness: .08, emissive: accent, emissiveIntensity: .1});
  const glow = new THREE.MeshStandardMaterial({color: accent, emissive: accent, emissiveIntensity: 2, roughness: .15});
  function mesh(geometry, material, x = 0, y = 0, z = 0) {
    const m = new THREE.Mesh(geometry, material);
    m.position.set(x, y, z);
    m.castShadow = true;
    m.receiveShadow = true;
    group.add(m);
    return m;
  }
  function box(w, h, d, mat, x, y, z, r) {return mesh(roundedBox(w, h, d, r), mat, x, y, z);}
  function ring(radius, tube, mat, z, y = .32) {
    return mesh(new THREE.TorusGeometry(radius, tube, 12, 64), mat, 0, y, z);
  }
  // A stylized mechanical camera; all geometry and labels are authored here.
  box(1.3, 1.96, .64, white, 0, .1, 0, .14);
  box(1.16, 1.82, .055, edge, 0, .1, .355, .1);
  box(1.03, 1.67, .045, white, 0, .11, .4, .06);
  box(.14, 1.58, .72, white, -.76, .17, -.07, .04);
  box(.14, 1.58, .72, white, .76, .17, -.07, .04);
  box(.095, .56, .15, dark, -.86, .15, .04, .02);
  box(.095, .56, .15, dark, .86, .15, .04, .02);
  box(.82, .2, .75, edge, 0, -1.01, -.05, .035);
  box(.55, .16, .52, dark, 0, -1.18, -.08, .03);
  const gimbal = mesh(new THREE.CylinderGeometry(.17, .17, .45, 24), edge, 0, -1.36, -.1);
  gimbal.rotation.z = Math.PI / 2;
  box(.2, .48, .26, edge, -.34, -1.3, -.12, .03);
  box(.2, .48, .26, edge, .34, -1.3, -.12, .03);
  box(.82, .1, .5, dark, 0, -1.58, -.12, .035);
  const back = box(.88, 1.1, .15, dark, 0, .08, -.4, .06);
  for (let i = 0; i < 8; i++) box(.055, 1.0, .045, edge, -.34 + i * .098, .08, -.5, .01);
  const outer = mesh(new THREE.CylinderGeometry(.54, .54, .085, 64), dark, 0, .35, .48);
  outer.rotation.x = Math.PI / 2;
  ring(.498, .028, edge, .55, .35);
  ring(.452, .032, black, .58, .35);
  const glass = mesh(new THREE.SphereGeometry(.42, 48, 28), lens, 0, .35, .57);
  glass.scale.z = .18;
  ring(.34, .009, dark, .65, .35);
  ring(.22, .008, glow, .665, .35);
  mesh(new THREE.CircleGeometry(.115, 48), glow, 0, .35, .675);
  mesh(new THREE.CircleGeometry(.045, 32), new THREE.MeshBasicMaterial({color: '#ffe2dc'}), 0, .35, .68);
  const reflection = mesh(new THREE.CircleGeometry(.045, 20), new THREE.MeshBasicMaterial({color: '#b7c6c6', transparent: true, opacity: .28}), -.16, .55, .65);
  for (const x of [-.46, .46]) for (const y of [-.64, .9]) {
    mesh(new THREE.CircleGeometry(.025, 12), dark, x, y, .435);
    box(.025, .006, .004, edge, x, y, .44, .001);
  }
  box(.72, .15, .025, dark, 0, -.38, .445, .015);
  for (let i = 0; i < 9; i++) box(.035, .08, .018, black, -.29 + i * .072, -.38, .468, .005);
  const labelCanvas = document.createElement('canvas');
  labelCanvas.width = 512; labelCanvas.height = 128;
  const ctx = labelCanvas.getContext('2d');
  ctx.fillStyle = '#c4c9c7'; ctx.fillRect(0, 0, 512, 128);
  ctx.fillStyle = '#273337'; ctx.font = '500 65px Bahnschrift, sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('550W', 256, 78);
  const texture = new THREE.CanvasTexture(labelCanvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  mesh(new THREE.PlaneGeometry(.62, .155), new THREE.MeshStandardMaterial({map: texture, roughness: .75}), 0, -.66, .464);
  box(.05, .018, .02, glow, -.26, -.81, .46, .003);
  box(.05, .018, .02, edge, -.17, -.81, .46, .003);
  box(.05, .018, .02, edge, -.08, -.81, .46, .003);
  group.userData.glow = glow;
  return group;
}

export class AvatarView {
  constructor(host, report) {
    this.host = host;
    this.report = report;
    this.state = 'idle'; this.target = {x: 0, y: 0}; this.generation = 0;
    this.fallback = host.querySelector('.avatar-fallback');
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.move = event => {
      const rect = host.getBoundingClientRect();
      this.target.x = ((event.clientX - rect.left) / rect.width - .5) * .23;
      this.target.y = ((event.clientY - rect.top) / rect.height - .5) * .1;
      this.requestFrame();
    };
    this.leave = () => {this.target = {x: 0, y: 0}; this.requestFrame();};
    this.visibility = () => {if (document.hidden) cancelAnimationFrame(this.frame); else this.requestFrame();};
    host.addEventListener('pointermove', this.move);
    host.addEventListener('pointerleave', this.leave);
    document.addEventListener('visibilitychange', this.visibility);
    this.motion = matchMedia('(prefers-reduced-motion: reduce)');
    this.motionChange = () => this.requestFrame();
    this.motion.addEventListener('change', this.motionChange);
  }

  async setProfile(profile) {
    this.animate = profile.layout.animate;
    const signature = JSON.stringify([profile.avatar, profile.accent]);
    if (signature === this.signature) {this.requestFrame(); return;}
    this.signature = signature;
    const generation = ++this.generation;
    this.clearScene();
    if (profile.avatar.kind === 'image') {
      const image = document.createElement('img');
      image.alt = `${profile.name} 的自定义形象`;
      image.src = `/api/presence/media/${profile.avatar.asset_id}`;
      image.addEventListener('error', () => {image.remove(); this.fallback.hidden = false; this.report('图片资源无法读取，显示备用形象', '2D FALLBACK');});
      this.host.append(image); this.image = image; this.fallback.hidden = true;
      this.report('自定义图片形象', 'IMAGE');
      return;
    }
    try {
      const renderer = new THREE.WebGLRenderer({alpha: true, antialias: true, powerPreference: 'low-power'});
      renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = profile.avatar.kind === 'licensed-moss' ||
        (profile.avatar.kind === 'procedural-moss' && profile.avatar.detail !== 'simple') ? 1.0 : 1.3;
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      this.renderer = renderer;
      this.host.append(renderer.domElement);
      const scene = new THREE.Scene(); this.scene = scene;
      this.camera = new THREE.PerspectiveCamera(30, 1, .1, 100);
      this.camera.position.set(0, .15, 7.6);
      this.camera.lookAt(0, -.1, 0);
      scene.add(new THREE.HemisphereLight('#e3edf2', '#43433c', 2.5));
      const key = new THREE.DirectionalLight('#f4eee1', 4);
      key.position.set(-3, 5, 4); key.castShadow = true; key.shadow.mapSize.set(1024, 1024);
      scene.add(key);
      const rim = new THREE.DirectionalLight('#a4bdce', 3);
      rim.position.set(3, 1, -2); scene.add(rim);
      const red = new THREE.PointLight(profile.accent, .35, 4); red.position.set(0, .4, 2); scene.add(red);
      const floor = new THREE.Mesh(new THREE.PlaneGeometry(12, 12), new THREE.ShadowMaterial({opacity: .24}));
      floor.rotation.x = -Math.PI / 2; floor.position.y = -1.78; floor.receiveShadow = true; scene.add(floor);
      const pivot = new THREE.Group(); this.pivot = pivot; scene.add(pivot);
      let object;
      if (profile.avatar.kind === 'glb' || profile.avatar.kind === 'licensed-moss') {
        const licensed = profile.avatar.kind === 'licensed-moss';
        object = (await new GLTFLoader().loadAsync(licensed ? '/presence/assets/models/moss-550w-jom.glb' : `/api/presence/media/${profile.avatar.asset_id}`)).scene;
        if (generation !== this.generation) {disposeObject(object); return;}
        const box = new THREE.Box3().setFromObject(object);
        const size = box.getSize(new THREE.Vector3());
        const max = Math.max(size.x, size.y, size.z);
        if (!Number.isFinite(max) || max <= 0) throw new Error('模型没有可显示的几何体');
        const center = box.getCenter(new THREE.Vector3());
        const scale = (licensed ? 3.15 : 2.7) / max;
        object.position.sub(center); object.scale.multiplyScalar(scale);
        // Position must use the same scale as the geometry.
        object.position.multiplyScalar(scale);
        object.traverse(node => {if (node.isMesh) {
          node.castShadow = !node.material?.transparent; node.receiveShadow = true;
          if (licensed && node.material?.emissiveMap) this.pulseMaterials.push([node.material, node.material.emissiveIntensity]);
        }});
      } else object = profile.avatar.detail === 'simple' ? makeSimpleMoss(profile.accent) : makeDetailedMoss(profile.accent);
      pivot.add(object); this.object = object;
      pivot.rotation.set(.035, -.24, -.045);
      this.fallback.hidden = true;
      const detailed = profile.avatar.kind === 'procedural-moss' && profile.avatar.detail !== 'simple';
      const licensed = profile.avatar.kind === 'licensed-moss';
      this.report(licensed ? `MOSS · Jom / CC BY 4.0${profile.avatar.credit ? ' · '+profile.avatar.credit : ''}` : profile.avatar.credit || (profile.avatar.kind === 'glb' ? '自定义三维形象' : detailed ? 'MOSS · 550W 详细版' : 'MOSS · 简版'),
        licensed ? 'MOSS 3D' : profile.avatar.kind === 'glb' ? 'CUSTOM GLB' : detailed ? 'DETAILED 3D' : 'PROCEDURAL 3D');
      this.resize();
    } catch (error) {
      if (generation !== this.generation) return;
      this.clearScene(); this.fallback.hidden = false;
      this.report(`三维不可用，显示二维备用外观 · ${error.message}`, '2D FALLBACK');
    }
  }

  setState(state) {this.state = state; this.requestFrame();}
  resize() {
    if (!this.renderer) return;
    const {width, height} = this.host.getBoundingClientRect();
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.position.z = this.camera.aspect < 1 ? 7.8 / this.camera.aspect : 7.6;
    this.camera.updateProjectionMatrix(); this.requestFrame();
  }
  requestFrame() {
    if (!this.renderer || document.hidden) return;
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(t => this.draw(t));
  }
  draw(time) {
    if (!this.renderer || document.hidden) return;
    const moving = this.animate && !this.motion.matches;
    if (!moving || !this.lastDraw || time - this.lastDraw >= 1000 / 30) {
      this.lastDraw = time;
      if (this.pivot) {
        const yaw = -.24 + (moving ? this.target.x + Math.sin(time / 4100) * .035 : 0);
        const pitch = .035 + (moving ? this.target.y + (this.state === 'working' ? .07 : 0) : 0);
        this.pivot.rotation.y += (yaw - this.pivot.rotation.y) * .06;
        this.pivot.rotation.x += (pitch - this.pivot.rotation.x) * .06;
        this.pivot.position.y = moving ? Math.sin(time / 2400) * .012 : 0;
        if (this.object?.userData.glow) this.object.userData.glow.emissiveIntensity = this.state === 'working' && moving ? 1.6 + Math.sin(time / 600) * .35 : 1.7;
        for (const [material, intensity] of this.pulseMaterials) material.emissiveIntensity = intensity * (this.state === 'working' && moving ? 1 + Math.sin(time / 600) * .12 : 1);
        const joint = this.object?.userData.pitchJoint;
        if (joint) {
          const pitchTarget = moving ? (this.state === 'working' ? .06 : 0) + this.target.y * .4 : 0;
          joint.rotation.x += (pitchTarget - joint.rotation.x) * .06;
        }
      }
      this.renderer.render(this.scene, this.camera);
    }
    if (moving) this.frame = requestAnimationFrame(t => this.draw(t));
  }
  clearScene() {
    cancelAnimationFrame(this.frame);
    disposeObject(this.scene); this.scene = null; this.pivot = null; this.object = null; this.pulseMaterials = [];
    this.renderer?.dispose(); this.renderer?.domElement.remove(); this.renderer = null;
    this.image?.remove(); this.image = null; this.lastDraw = 0;
  }
  dispose() {
    ++this.generation; this.clearScene(); this.resizeObserver.disconnect();
    this.host.removeEventListener('pointermove', this.move);
    this.host.removeEventListener('pointerleave', this.leave);
    document.removeEventListener('visibilitychange', this.visibility);
    this.motion.removeEventListener('change', this.motionChange);
  }
}
